# Deployment retries и отзывчивость GraphQL Coverage

База: `main`, `e6504cb892081f3a99d21eac825967478f45cfde`.
Реализация и тесты: `70848471e7359b7d775eae0f4918393667227000`.
Финальный SHA документационного коммита и ссылка на PR приведены в итоговом
сообщении и GitHub PR. Ветка: `codex/deploy-retries-event-loop`.
Исходное рабочее дерево оставлено без изменений; использован чистый клон
`/tmp/graphql-reliability`, поскольку в исходном каталоге были другая ветка и
посторонние untracked-файлы.

## Исправления

Deployment хранит последний успешный SHA в root-only
`/var/lib/graphql-coverage-deploy/last-successful-sha`. Запись атомарно заменяется
**после** успешных `/health` и `/api/services`; перед изменением checkout/API
предыдущая запись удаляется. Отсутствующая, пустая, неверная или неизвестная Git
запись требует полноценного повторного deployment. Один HEAD больше не даёт
успех: после build/up/readiness-сбоя повтор того же SHA снова выполняет backup,
build, up и readiness. Даже подтверждённый SHA пропускает эти операции только
при совпадающем HEAD и успешной повторной проверке API. Старые и расходящиеся
SHA отклоняются; сохранены полная SHA-проверка, origin/main ancestry, ff-only,
чистота tracked-файлов, root-проверка и `flock`.

Перед миграциями остаются `pg_dump` и проверка `pg_restore -l`; контейнер БД и
volume не пересоздаются, автоматического восстановления БД нет. Readiness
проверяет текущее состояние API; как и прежде, это не механизм привязки образа
к OCI revision и не гарантия будущей доступности после проверки.

Чистые вычисления SDL/GraphQL schema, metadata и построения GraphField/GraphType/
GraphRelation DTO выполняются через общий `schema_work`: `asyncio.to_thread`,
не более двух заданий одновременно на event loop процесса, по образцу
существующего ограничения password work через AnyIO RunVar. Introspection
использует тот же CPU-лимит, сохраняя внешний лимит outbound. Ожидающие запросы
отменяются без запуска вычислений; выполняющийся поток после отмены сохраняет
слот до реального завершения. Его результат отбрасывается, запись не начинается.
Сеансы/ORM-модели в потоки не передаются: граф получает только scalar SQL rows,
строки, UUID и counts. Python threads не устраняют GIL и не дают линейного
ускорения CPU; цель — убрать длительное синхронное вычисление из event loop и
ограничить число заданий.

HTTP SDL import имеет отдельные короткие фазы чтения и записи, как прежний
introspection import: соединение освобождается до ожидания CPU; под service lock
повторяются актуальная аутентификация/роль/CSRF/ACL, проверка существования и
savepoint. Граф сначала загружает plain data в scoped session, закрывает её и
только затем ожидает CPU/строит DTO. Внутренние функции, принимающие caller-owned
AsyncSession, сохраняют владение и границы транзакции вызывающего кода: не делают
скрытый commit/rollback ради освобождения соединения. HTTP использует фабрику
коротких сессий и не держит DB connection во время вычислений.

`/stats` агрегирует только нужные counts в PostgreSQL и не строит GraphQL schema,
граф, аргументы, описания и relations. Общие с графом `counted_observation`,
`COVERAGE_KINDS` и `coverage_summary` сохраняют eligibility, OBJECT denominator,
округление и zero semantics. Исторические алгоритмы coverage, DTO и HTTP-коды
не изменялись. Две HTTP-fixture получили подмену фабрики сессий для нового
жизненного цикла; прежние тесты сохранены.

## Доказательства регрессий

- `tests/test_deploy_retries.py`: 21 тест. A — успешная запись после readiness;
  B — build failure и повторная ошибка/восстановление на том же HEAD;
  C — up/readiness failure с тем же повтором; D — подтверждённый повтор выполняет
  только port/health/read API; E — формат/существование/принадлежность SHA,
  запрет rollback/divergence/dirty tree. Дополнительно: broken/missing record,
  backup failure, занятый flock и повторная потеря readiness после успеха.
- `tests/test_schema_work.py`: 7 тестов. Реальный parse 30 000 полей в worker
  thread и handshake с другой coroutine; 12 запросов, отмена выполняющихся и
  ожидающих работ, затем ещё 4 запроса с контролем peak; ограничения типов,
  50 000 полей и 1 000 000 символов; сохранение ошибок parser.
- `tests/test_schema_work_postgres.py`: 6 тестов. HTTP import 30 000 полей;
  parser/GraphQL build вне event loop, pool.checkedout()==0 и отдельный health
  во время CPU pause; отмена парсинга и после flush без частичных schema/type/field
  записей; отзыв ACL до сохранения; generic 422/404; stats/graph parity на
  OBJECT/INTERFACE, повторных hits, response_has_data=false и failed analysis,
  неизменный JSON /stats без вызова schema metadata или GraphField.

Handshake/thread identity/peak и состояние pool — критерии успеха. Таймауты
10–15 секунд используются только как защита от зависания теста, не как benchmark
или жёсткий порог задержки event loop.

На исходном `e6504cb` финальные deployment-тесты: **13 failed, 8 passed**.
Все три build/up/readiness retry-теста возвращают ложный успех на повторе.
На чистой исходной Python-реализации тест parser scheduling падает с
`SDL parser blocked the event loop`, а stats-тест — на запрете вызова schema
metadata. Это behavioral failures, не ImportError новых helper-модулей.

## Проверки

| Проверка | Результат |
| --- | --- |
| Полный общий run до последнего shell hardening | 574 passed, 0 skipped (226.70 s) |
| Финальный полный unit-набор после добавления ещё 3 shell cases | 360 passed, 217 integration deselected (21.31 s) |
| PostgreSQL integration в общем run, Python-код после него не менялся | 217 passed, 0 skipped |
| Новые регрессии | 34 passed: deployment 21, CPU/limits 7, PostgreSQL 6 |
| Python / PostgreSQL | 3.12.3 / 16.15, отдельный localhost:56546, свежие тестовые БД |
| `pip check` | No broken requirements found |
| `bash -n` deployment script | passed |
| Dev и production `docker compose config --quiet` | passed; production с фиктивным env |
| 5 Bash blocks checklist | bash -n passed, команды на VPS не выполнялись |
| `git diff --check` | passed |

В сумме финальные unit + integration покрывают 577 тестов. Последние три tests
добавлены только для строгого byte format success journal (NUL, отсутствующий
или лишний newline); после них повторён весь unit-набор. Python-код и integration
fixtures после полного успешного общего run не менялись. Единственное
предупреждение полного run — существующее Starlette/httpx deprecation.
Зависимости окружения: graphql-core 3.3.2, SQLAlchemy 2.1.8, asyncpg 0.32.0,
AnyIO 4.16.0, FastAPI 0.163.1, pytest 9.2.0. PostgreSQL 16 получен из Ubuntu
пакетов, распакован и запущен в `/tmp`; системные службы и Docker volumes
не использовались.

Воспроизведение при доступном отдельном PostgreSQL (никогда production):

```bash
python3.12 -m venv /tmp/graphql-reliability-venv
/tmp/graphql-reliability-venv/bin/pip install -e '.[test]'
# DATABASE_URL и TEST_DATABASE_URL должны указывать на разные пустые локальные
# БД тестового сервера. Integration fixtures создают/удаляют свои временные БД.
DATABASE_URL="$TEST_DATABASE_URL" /tmp/graphql-reliability-venv/bin/alembic upgrade head
/tmp/graphql-reliability-venv/bin/python -m pytest -m 'not integration' -q
/tmp/graphql-reliability-venv/bin/python -m pytest -m integration -q
/tmp/graphql-reliability-venv/bin/python -m pip check
bash -n deploy/vps/graphql-coverage-deploy.sh
docker compose config --quiet
POSTGRES_DB=coverage_check POSTGRES_USER=coverage_check \
POSTGRES_PASSWORD=test_only_not_a_secret FORWARDED_ALLOW_IPS=172.20.0.1 \
  docker compose --env-file /dev/null -f deploy/compose.production.yaml config --quiet
```

## Ограничения и отдельная проблема

Локальный Docker daemon недоступен даже вне sandbox (`Cannot connect to the
Docker daemon`). Реального Docker image build, Docker/Nginx/auth smoke и
production load test локально не было. Compose config проверен настоящим CLI;
подмены Docker в shell-регрессиях проверяют orchestration/retry и не заменяют
container smoke. Существующий PR CI должен отдельно выполнить Docker smoke.
VPS, production БД и установленный скрипт не проверялись и не изменялись.
Отмена покрыта во время CPU и после flush; остановка хоста/ОС во время atomic
rename и каждый возможный сбой файловой системы не моделировались.

Обнаружено вне объёма PR: `type Query { invalid: Unknown }` приводит в graphql-core
к `TypeError`, который прежний SDL HTTP handler не преобразует в generic 422
(получается 500). Поведение сохранено; исправление и отдельный regression test
следует вынести в другой PR. Зависимости, logging и общий cleanup fixtures
не менялись.

## Изменённые файлы

- `app/api.py` — короткие HTTP sessions и повторная авторизация SDL write.
- `app/services.py` — отделение загрузки scalar data от CPU graph builder,
  offload SDL и общие coverage primitives.
- `app/schema_work.py` — bounded CPU helper с cancellation-safe lifetime слота.
- `app/schema_collector.py` — общий CPU-лимит для introspection normalization.
- `app/run_analytics.py` — summary counts без полного графа.
- `deploy/vps/graphql-coverage-deploy.sh` — атомарный success journal и retry.
- `tests/test_deploy_retries.py`, `tests/test_schema_work.py`,
  `tests/test_schema_work_postgres.py` — новые регрессии.
- `tests/test_final_features_postgres.py`, `tests/test_proxy_e2e_postgres.py` —
  точечное подключение session factory к прежним HTTP tests.
- `deploy/AUTODEPLOY.md` — ссылка на порядок установки до merge.
- `deploy/RELIABILITY-RELEASE.md` — готовый Production Pre-Deployment Checklist.
- `docs/reliability-fixes-report.md` — этот отчёт.

## Ручной контроль выпуска

Следовать [Production Pre-Deployment Checklist](../deploy/RELIABILITY-RELEASE.md).
Он содержит точный commit SHA/контрольную сумму, получение файла в root-only
staging, bash -n, права, backup старой копии, atomic install под существующим
flock, условия остановки, восстановление и точку разрешения merge.
Новый journal вручную не инициализируется из HEAD. Установка нового файла сама
по себе не изменяет приложение; первый merge запускает существующий Actions
workflow, который создаёт journal после readiness.

Ответственному проверить независимое ревью/CI, отсутствие старых Actions runs,
временную блокировку push/merge/manual dispatch при замене файла, root ownership,
checksum установленной копии, неизменность API до merge, backup БД и соответствие
journal merge-SHA после первого autodeploy, внешние login/coverage/stats через
существующий Nginx. Merge/deploy, обновление root-owned копии и любые действия с
production выполняет только оператор; агент этого не делал.
