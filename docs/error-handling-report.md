# P2: безопасная диагностика и классификация невалидного SDL

База проверена перед началом: актуальный `main` —
`7c7f09354623d6d03ca64453d93daf944a7c62c3` (после PR #29).
Рабочая ветка: `codex/safe-error-diagnostics`, отдельное чистое worktree
`/tmp/graphql-error-diagnostics`. Исходный каталог с посторонними untracked-файлами
и прежняя ветка не менялись. Итоговый commit SHA, PR и результат его CI указаны
в описании PR и итоговом сообщении после завершения проверок.

## Подтверждённые причины

1. `capture_e2e_exchange` ловил ошибки нормализации документа и вызова анализатора,
   возвращая unavailable/internal_error без диагностического события.
   Кроме того, `analyze_query`, `normalize_document` и `mask_value_ast` сами
   поглощали исключения, поэтому логирования только внешнего catch недостаточно.
2. Proxy преобразовывал сбои preflight/storage в безопасные 503/502, сетевые
   ошибки — в 502/504. SDL/introspection handlers преобразовывали SQLAlchemyError
   в 503. Класс и этап исходной ошибки терялись.
3. `graphql-core` выполняет `assert_valid_sdl` внутри `build_schema` и выбрасывает
   `TypeError` для неизвестного типа, неизвестной директивы и дублирующегося поля.
   SDL HTTP handler ожидает ValueError/GraphQLError/RecursionError, поэтому
   возникал необработанный TypeError и HTTP 500.

## Реализация и границы безопасности

Добавлен небольшой общий helper `app.diagnostics`. Событие уровня ERROR:

```json
{"event":"coverage_internal_failure","stage":"observation_save","exception_type":"IntegrityError","run_id":"00000000-0000-0000-0000-000000000001","sqlstate":"23514"}
```

Формат — JSON в обычном message и тот же словарь в `LogRecord.diagnostic`.
Он виден и без нового formatter/root logging config. Поля собираются по allowlist:
фиксированные event/stage, класс исключения, UUID run/service при наличии.
SQLSTATE читается только у `DBAPIError.orig.sqlstate` и допускается только как
пять ASCII-символов `[0-9A-Z]`. SQLSTATE не извлекается из текста или cause chain.
Operation names и query hashes намеренно не используются: для корреляции хватает
run UUID, а имена могут содержать пользовательские данные.

Ни exception object, ни `str`/`repr(exc)`, ни traceback/locals, SQL statement,
SQL-параметры, query/variables, тела request/response, URL, credentials или cookies
не передаются logger. `logger.exception`/`exc_info` не используются. Изменения
не включают глобальный DEBUG и не перенастраивают сторонние HTTP/SQL loggers.

Этапы: `coverage_preflight`, `upstream_request`, `observation_save`, `analyzer`,
`safe_document`, `safe_document_parse`, `safe_document_mask`,
`sdl_import_storage`, `introspection_import_storage`.
ContextVar передаёт только run UUID внутрь analyzer/document worker threads через
существующий `asyncio.to_thread`; token сбрасывается и при отмене. Внутренние
catch пишут событие перед прежним fallback. Обычные malformed documents,
Unicode/recursion/AST limits и отклонённый доступ не становятся internal failures.
Отдельный приватный тип исключения отличает ожидаемый AST limit от неожиданного
ValueError принтера — последний также диагностируется.

SDL теперь проходит `parse` → `validate_sdl` → `build_ast_schema` → прежний
`validate_schema` и прежние ограничения типов/полей. AST парсится один раз;
`assume_valid_sdl=True` используется только после явной SDL-валидации, а не
для её отключения. Невалидный результат `validate_sdl` даёт фиксированный
ValueError, который прежний HTTP handler переводит в generic 422. Нет глобального
`except TypeError`: произвольный TypeError builder/ORM не выдаётся за ошибку SDL.
Работа по-прежнему выполняется вне event loop через механизм PR #29.

Сохранены публичные DTO, сообщения 502/503/504 и коды internal_error,
авторизация, ACL/CSRF, транзакции, coverage и запрет автоматического upstream retry.
При сбое optional safe document upstream response возвращается как раньше;
при сбое анализатора записывается прежний internal_error без выдуманных fields;
при сбое persistence после upstream остаётся прежний 502/Do not retry automatically.

## Регрессии и red/green

Новые тесты:

- `tests/test_error_diagnostics.py`: 19 cases. Внешний/внутренний analyzer failure,
  wrapper/parser/printer safe-document failure, preflight, observation save,
  upstream timeout/connect error, оба schema storage handler; точное безопасное
  содержимое события; валидация SQLSTATE/UUID; TypeError builder/storage не
  маскируется; отмена не преобразуется в internal_error.
- `tests/test_error_diagnostics_postgres.py`: реальный CHECK violation/SQLSTATE
  23514 после одного upstream-вызова; точный 502, rollback без observations,
  нет retry, SQL statement/параметров/секретов в событии.
- `tests/test_sdl_errors_postgres.py`: 10 cases. Реальный HTTP 422 для неизвестного
  типа/директивы и дублирующегося поля; generic detail, прежние counts
  schema/type/field; anonymous/CSRF/Origin/отозванный grant/роль/несуществующий
  service отвергаются до парсинга; storage TypeError не становится 422.
- Прежний printer privacy test в `tests/test_safe_document.py` сохранён и усилен:
  вместо требования полного молчания проверяет safe event без source/traceback;
  добавлен отдельный вариант ValueError. Прежние parse/limit tests сохранены.

Exceptions в security tests содержат Cookie/token/password/API key, variables,
query/response и URL query-string canaries. Специальный exception запрещает
вызов собственного `__str__` и `__repr__`. Проверяется весь диагностический
LogRecord, а не только публичный ответ. Корреляция run UUID проверяется также
для ошибок из worker thread. Потенциально выполненная mutation отправляется
upstream ровно один раз; cancellation не запускает сохранение.

На чистом исходном `7c7f093`:

- выбранные behavioral unit tests: **12 failed**, 7 deselected — отсутствуют
  события или UnknownType даёт TypeError. Это не ImportError нового helper.
- новые PostgreSQL tests: **4 failed, 7 passed** — три фактических HTTP 500 вместо
  422 и отсутствие события при настоящем DB write rejection. Семь тестов
  существующих ограничений доступа/TypeError semantics проходят и на базе.

После исправления все указанные случаи проходят. Новых regression cases — 31
(19 unit + 11 integration + один дополнительный вариант прежнего printer test).

## Проверки

| Проверка | Результат |
| --- | --- |
| Полный unit + PostgreSQL run | 607 passed, 0 skipped; 238.45 s |
| Финальный полный unit/security набор после добавления printer ValueError case | 380 passed, 228 integration deselected; 18.75 s |
| PostgreSQL integration в полном run | 228 passed, 0 skipped |
| Targeted real DB/SDL regression run | 11 passed |
| graphql-core 3.2.14: diagnostics + schema parser + safe document | 51 passed |
| `pip check` | No broken requirements found |
| `compileall app`, `git diff --check` | passed |
| Dev и production Compose config | passed, production с фиктивными значениями env |
| Local Docker/Nginx smoke | не выполнялся: daemon недоступен даже вне sandbox |

Финальная совокупность — 608 тестов: 380 unit + 228 integration. Полный общий
run содержал 607 tests; после дополнительного различения AST limit и printer
ValueError повторён весь unit-набор, включая этот новый case. Никакие business/
DB/HTTP paths после полного общего run не изменялись. Единственное warning
полного run — прежнее Starlette/httpx deprecation.
Окружение: Python 3.12.3, PostgreSQL 16.15 (отдельные свежие diagnostics_app /
diagnostics_tests на localhost:56546 из временных PostgreSQL binaries в /tmp),
graphql-core 3.3.2, SQLAlchemy 2.1.8, asyncpg 0.32.0, pytest 9.2.0.
Дополнительная версия graphql-core установлена только в /tmp target directory;
pyproject и рабочее окружение зависимостей не менялись.

Воспроизведение (только изолированные БД, никогда production):

```bash
python3.12 -m venv /tmp/coverage-diagnostics-venv
/tmp/coverage-diagnostics-venv/bin/pip install -e '.[test]'
# Задать разные DATABASE_URL / TEST_DATABASE_URL для локальных пустых БД.
/tmp/coverage-diagnostics-venv/bin/alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" /tmp/coverage-diagnostics-venv/bin/alembic upgrade head
/tmp/coverage-diagnostics-venv/bin/python -m pytest -m 'not integration' -q
/tmp/coverage-diagnostics-venv/bin/python -m pytest -m integration -q
/tmp/coverage-diagnostics-venv/bin/python -m pip check
```

Проверки CI финального SHA (включая реальный Docker/Nginx/Uvicorn smoke) и ссылка
на Actions run фиксируются в PR description и итоговом сообщении. Никакой
локальный Docker smoke не объявляется успешным при недоступном daemon.

## Изменённые файлы

- `app/diagnostics.py`: allowlisted events и UUID context.
- `app/proxy.py`: диагностика уже существующих safe HTTP boundaries.
- `app/e2e_capture.py`: analyzer/document failures и run context.
- `app/query_analyzer.py`: внутренний catch до internal_error.
- `app/safe_document.py`: неожиданные parser/printer failures без source.
- `app/api.py`: SDL/introspection storage failures без SQL values.
- `app/schema_parser.py`: явная классификация ошибок SDL-валидации.
- `tests/test_error_diagnostics.py`, `tests/test_error_diagnostics_postgres.py`,
  `tests/test_sdl_errors_postgres.py`, `tests/test_safe_document.py`: регрессии.
- `docs/error-handling-report.md`: этот отчёт.

## Остаточные риски и PR #31

Безопасные события дают stage/type/UUID/SQLSTATE, но намеренно не содержат
exception messages и traceback. Сбор, доставка и retention production logs
зависят от действующей инфраструктуры и в этом PR не меняются. Стороннее
debug/SQL echo/APM body logging не включалось и не аудировалось как отдельная
система; не следует считать эти события универсальным фильтром чужих логов.
Ожидаемые malformed/limit отказы не логируются как внутренние аварии.
Произвольные TypeError намеренно не переводятся массово в 422.
Production load/fault injection не выполнялись; никаких действий с VPS нет.

Для **PR #31** оставлена отдельная функциональная задача GraphQL error details
по ТЗ капитана: её согласованный формат, безопасная обработка/редакция данных,
хранение/выдача согласно этому отдельному контракту и соответствующие ACL/privacy
тесты. Этот PR не выбирает новый формат и не реализует error.message/path/
extensions API, не добавляет columns/DTO/endpoints. Существующие envelope flags
и агрегаты ошибок остаются прежними. PR #31 на момент начала работы ещё не
существовал; создавать его или расширять текущий PR не требовалось.

Модели БД, Alembic, Nginx, Docker Compose, deployment script, Variables/Secrets
не изменены. Merge, deployment и подключение к production не выполнялись.

## Исправление замечания независимого ревью PR #30: best-effort диагностика

Проверенный HEAD до исправления:
`b8098434157ea4638a993a2a5b1a21cd9726f2f6`. Работа продолжена в той же ветке
`codex/safe-error-diagnostics`; обновляется существующий PR #30.

### Причина и решение

Первоначальный helper выполнял чтение SQLSTATE и `logger.error` внутри исходного
`except`. Исключение accessor/handler прерывало возврат безопасного ответа.
Кроме того, стандартный `StreamHandler.emit` сам ловит ошибки formatter/write/
flush и вызывает `handleError`: при `logging.raiseExceptions=True` traceback
может включать контекст исходного DBAPIError с SQL, параметрами и секретами.
Поэтому одного внешнего `except` вокруг logger недостаточно.

`app/diagnostics.py` теперь собирает прежние allowlisted метаданные best effort.
Сбой SQLSTATE accessor оставляет событие без необязательного SQLSTATE. Ошибки
сериализации/очереди поглощаются без fallback-вывода. `BaseException`, включая
`CancelledError`, на request path не перехватывается.

В очередь размером 128 помещаются только JSON message и словарь безопасных
метаданных. Exception object, traceback и request context не передаются.
Один daemon thread на процесс отправляет события из отдельного контекста;
блокирующий sink не выполняет I/O на event loop. Очередь не создаёт новые
потоки для запросов и не ждёт освобождения места: при переполнении событие
отбрасывается. Ошибка handler/filter не останавливает обработку HTTP или
consumer. Повторных upstream-вызовов нет.

Изолированный logger `app.diagnostics` уровня ERROR имеет собственный
`_SafeStreamHandler` и `propagate=False`: непроверенные parent/root handlers и
lastResort не используются. Handler подавляет ошибки emit, а его `handleError`
намеренно ничего не печатает. Глобальный `logging.raiseExceptions` не меняется.
Штатный JSON и `LogRecord.diagnostic`, stage/type/UUID/SQLSTATE сохранены.
Реальная конфигурация `app.server.server_config().load()` с повторным
`configure_logging()` проверена subprocess-тестом: событие видно ровно один раз.

### Файлы и новые тесты

В этом follow-up изменены только:

- `app/diagnostics.py`: защищённый logging path и bounded best-effort доставка.
- `tests/conftest.py`: pytest caplog явно подключён к изолированному logger;
  ограниченное ожидание доставки выполняется только при инспекции тестовых
  записей/teardown. Прежние проверки конфиденциальности сохранены.
- `tests/test_diagnostics_best_effort.py`: 15 новых cases на HTTP boundaries.
- `docs/error-handling-report.md`: результаты и ограничения этого исправления.

Новые cases покрывают: OSError в emit; исключение SQLSTATE accessor;
formatter/write/stderr flush с `logging.raiseExceptions=True`; прежние
502/503/504 и analyzer/document fallback; ошибку enqueue; заблокированный sink
и заполненную очередь; 12 конкурентных run UUID, отмену одного запроса и сброс
ContextVar; реальную конфигурацию Uvicorn. Синхронизация конкурентности основана
на Event/barrier, а watchdog timeout только прерывает зависание. Тест не требует
достижения произвольного порога latency. Проверяются число upstream-вызовов,
точные HTTP-коды/тела, лог и stderr с canaries SQL/параметров/секретов. Успешный
proxy response по-прежнему передаёт upstream body без изменений; запрет
раскрытия body относится к диагностике и безопасным error responses.

До изменения production-кода добавлены и выполнены первые пять HTTP-регрессий:
**5 failed** на `b809843`: два фактических HTTP 500 вместо 502 и три случая
SQL/parameters/exception-message leakage в stderr. После исправления эти
случаи проходят; прежние privacy assertions не ослаблялись.

### Проверки follow-up

Полный локальный run: **623 passed, 0 skipped**, 248.59 s
(395 unit/security + 228 PostgreSQL integration). Единственное warning —
прежнее Starlette/httpx deprecation. Финальный CI указан в описании PR и
итоговом отчёте. Дополнительный run с graphql-core 3.2.14:
**66 passed** (новые регрессии, прежняя диагностика, parser и safe document).
`pip check`, компиляция изменённых Python-файлов и `git diff --check` проходят.
Полный набор использует Python 3.12 и PostgreSQL 16 с отдельными свежими
`best_effort_app` / `best_effort_tests` на localhost:56546.
Docker daemon локально недоступен и вне sandbox; локальный Docker smoke не
выполнен. Реальный Docker/Nginx/Uvicorn auth smoke проверяется GitHub Actions.

### Остаточные ограничения

Доставка best effort не является durable audit log: очередь может терять события
при переполнении, неисправном sink или завершении процесса. Request path не
ожидает flush/join. Проверена существующая конфигурация Uvicorn; сторонние
pre-fork hosts и внешняя перенастройка private logger не являются проверенными
deployment-конфигурациями. Production fault injection/load test не проводились.

Отдельная подтверждённая задача, существовавшая до PR #30: на graphql-core
3.2.14 `type Query { f: In } input In { x: String }` вызывает TypeError при
построении схемы и может дать HTTP 500. Она намеренно не исправлена здесь:
PR #30 не обещает классификацию всех вариантов невалидного SDL. Это отдельная
задача от GraphQL error details для PR #31, описанной выше.

Модели БД, миграции, ACL/CSRF/SSRF, транзакции, публичные контракты, Nginx,
Compose, deployment scripts и GitHub Variables/Secrets не менялись.
Merge, production deployment и подключения к production не выполнялись.

## Исправление P2/B2: завершение процесса с заблокированным sink

База повторного ревью — `128974cf73a5d2c0ceb1128808c3ee1bb5504f7f`.
B1 подтверждён исправленным; этот follow-up устраняет только новый B2 в PR #30.

### Root cause и жизненный цикл

`logging.Handler.handle()` держал handler lock на протяжении emit, включая
потенциально бесконечные stderr.write/flush в daemon consumer. На выходе Python
автоматически вызывает `logging.shutdown()`, который захватывает тот же lock и
вызывает flush. Поэтому daemon=True не обеспечивал выход процесса даже после
успешного ASGI lifespan shutdown и возврата Uvicorn.

Сохранены один consumer и очередь на 128 событий. Только этот consumer пишет
через приватный `_SafeStreamHandler`; его `createLock()` теперь устанавливает
lock=None. Shutdown-метод handler.flush() не выполняет I/O. Consumer сам вызывает
write и flush в emit, с прежним silent best-effort catch. Унаследованный
StreamHandler/Handler.close выполняет bookkeeping, не flush/close потока.
Никаких join, новых shutdown hooks, os._exit или глобальных logging-подмен нет.

Handler владеет отдельным Python text buffer поверх stderr fd 2 с
closefd=False. Это позволяет писать прежний JSON в тот же production stderr,
не удерживая buffer lock глобального sys.stderr при блокировке consumer.
Сам fd 2 не закрывается handler. Если stderr недоступен при создании буфера,
события отбрасываются без fallback. Только один consumer обращается к буферу,
поэтому handler lock не нужен для сериализации. Буфер не добавляет потоков,
очередей или неограниченного накопления. При зависшем I/O события остаются
ограничены размером очереди и могут теряться; завершение не ждёт их доставки.

Защита B1 сохранена: безопасные JSON/LogRecord.diagnostic, allowlist метаданных,
защищённый SQLSTATE accessor, отсутствие original exception/context в очереди,
propagate=False, silent handleError и никаких изменений logging.raiseExceptions.
Авторизация, ACL/CSRF/SSRF, транзакции и запрет mutation retry не менялись.

### Red/green и реальные shutdown tests

Добавлен `tests/test_diagnostics_shutdown.py`: **6 cases** — none/write/flush
для отдельного Python subprocess и локального Uvicorn с production
server_config. Uvicorn Config создаётся до импорта app.main, как в production:
иной порядок может удалить handler из logging shutdown registry и скрыть B2.

На неизменённом `128974c`: **4 failed, 2 passed**, 50.34 s. Все четыре случая
заблокированного sink зависли после MAIN_RETURNED; Uvicorn-варианты также
подтвердили ASGI_LIFESPAN_STOPPED и UVICORN_STOPPED. Обычный выход проходил.
12-секундный watchdog убил и reap-нул каждый зависший дочерний процесс.
Тесты не оставили зависших Python/Uvicorn процессов.

После исправления те же subprocess проходят с exit code 0 без kill/watchdog.
Uvicorn обслуживает реальный HTTP /health при заблокированной диагностике,
завершает реальный lifespan приложения (включая engine.dispose), останавливает
listeners по server.should_exit, возвращает serve и действительно выходит из
Python. Тест проверяет stdout markers, returncode и отсутствие SQL/parameter/
exception-token canaries в stdout/stderr. Глобальный process stderr перед выходом
восстанавливается: навсегда заблокирован именно diagnostic sink, а не чужие
Uvicorn handlers или стандартные потоки процесса в целом.

Дополнительный smoke с реально заполненным OS pipe и незавершённым write
consumer также завершился с exit code 0 после возврата main.

Целевой run B2 + B1 + прежняя diagnostics/privacy: **67 passed**, 13.70 s.
С graphql-core 3.2.14, включая schema parser: **72 passed**, 12.75 s.
Прежние 15 B1 cases сохранены. Их stderr capture изменён на capfd: новый
приватный буфер пишет в fd 2, и тест теперь проверяет фактический descriptor
output, а не только подменённый Python sys.stderr. Privacy assertions не удалены.

### Файлы и проверки

Изменены только `app/diagnostics.py`, `tests/test_diagnostics_best_effort.py`,
новый `tests/test_diagnostics_shutdown.py` и этот отчёт. `tests/conftest.py`
и остальные проверки не менялись. Полный локальный run с Python 3.12 и
PostgreSQL 16 использует две новые localhost БД shutdown_app/shutdown_tests:
**629 passed, 0 skipped**, 241.65 s (401 unit/security + 228 integration).
Единственное warning — прежнее Starlette/httpx deprecation.
CI run/commit SHA фиксируются в PR description и итоговом сообщении. pip check, compileall изменённых Python-файлов и diff whitespace
check проходят. Локальный Docker daemon остаётся недоступен; Docker smoke
подтверждается только фактическим GitHub Actions на итоговом HEAD.

### Ограничения

Доставка остаётся best effort без гарантии flush на выходе. Проверены текущая
production-конфигурация Uvicorn и стандартный diagnostic handler. Сторонняя
замена private logger/handlers, pre-fork hosts и зависания чужих logging paths
не исправляются глобально; такие конфигурации не вводились и не проверялись.
Production fault injection/restart/deployment не выполнялись.
Старый SDL input-in-output TypeError graphql-core 3.2.14 и GraphQL error details
для PR #31 остаются вне текущего исправления. DB models/migrations, Compose,
Nginx, deployment scripts, GitHub Variables/Secrets не менялись.
