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
