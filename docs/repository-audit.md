# Полный аудит репозитория перед ревью PR #31

16 ноября 2026, Asia/Yekaterinburg. [PR #31](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-Beckend/pull/31).
Аудирован весь Git-инвентарь исходного HEAD
`346ed4ce42fb657eb32cbc79df2f1daf281404f5`: **129 отслеживаемых файлов**,
18 179 строк. Проверка не ограничивалась diff PR. После добавления этого отчёта
и указателя документации — **131 файл**. Изменены 42 существующих файла,
добавлены два; итого **44 файла**, удалённых файлов нет.

Работа выполнена в чистом `/tmp/graphql-gap01`, на существующей ветке
`codex/safe-graphql-error-details`. Исходная папка на `chore/backend-ci`, её
`.worktrees/` и неотслеживаемый `docs/e2e-integration.md` сохранены. Новые ветки,
теги, merge и deploy не создавались; production не изменялся.

## Этап A — результаты до изменений

Для каждого файла проверены назначение, комментарии/docstrings, зависимости и
ссылки; для Python дополнительно AST, импорты, определения/вызовы, fixtures,
assertions и совпадение тел тестов. Для документации отделены текущие контракты
от исторических решений. Миграции сопоставлены с ORM, deployment — с Compose,
переменными окружения и workflow. Временный исходный инвентарь содержит
SHA-256/число строк каждого файла; проверяемая база закреплена выше.

| Категория | Находки и решение |
|---|---|
| Безопасная очистка | Исторические вводные docstrings; очевидный комментарий установки analyzer; повтор `*.egg-info/`; 14 неиспользуемых импортов в 8 тестовых модулях; повтор API/coverage инструкций в README и E2E |
| Необходимое исправление документации | E2E ошибочно заявлял отсутствие авторизации, 200 для неизвестного parent и обычный passthrough без run ID; REST-пример runner не передавал ключ. Deploy README описывал глобальный upstream вместо Service endpoint. Frontend-контракты обещали уже реализованные функции «позже» |
| Потенциально опасное изменение | Объединение DB fixtures, разбиение крупных API/services/сквозных тестов, удаление compatibility helpers, изменение dependency policy и release policy оставлены отдельными задачами |
| Исторический файл — сохранить | Все 8 revisions Alembic, 9 документов решений/отчётов, 4 release procedures с migration/rollback/security данными и проверенной checksum deploy script |

До этапа B перечень категорий и исходный инвентарь сохранены отдельно в `/tmp`.

## Этап B — выполненная очистка

- README переписан как вводная для разработчика: описание, возможности,
  архитектура, запуск, конфигурация, тесты, deployment, безопасность и контракты.
  **333 → 168 строк; 25 192 → 10 838 UTF-8 байт** (−57% по байтам).
  Убраны непроверенные заявления о состоянии frontend/production и этапах разработки.
- В `app/access.py` сокращён большой вводный блок, сохранён запрет пересылки CLI key.
  В `contracts.py` и `services.py` убраны «future proxy» и повтор названий;
  сохранены правила empty fields, атомарности и run lock. В `proxy.py` обновлён
  комментарий cookie isolation, в `query_analyzer.py` — причина unsupported selection.
  Очевидный комментарий в `main.py` удалён. В `tests/conftest.py` сокращены пояснения
  capture, сохранена причина отдельного logger и ожидания background emission.
- Удалены обычные неиспользуемые импорты: `text`; два `Run`; `finish_run` в двух
  модулях; `PinnedTransport`; `AsyncSession`; `api`; `app`; `ObservedRequest`;
  `RequestFieldUsage`; `import_introspection_schema`; `SchemaEndpointChanged`;
  `PinnedBackend`. Определения, fixtures, decorators, assertions и тесты не удалены.
- Неиспользуемых production imports для безопасного удаления не обнаружено:
  Alembic/ORM imports регистрируют таблицы, `schema_metadata` является re-export.
  Импорты fixtures, включая транзитивный `isolated_database`, нужны pytest.
  Единственная fixture без ссылки в аргументах — `configured_admin_key`, она autouse.
  Идентичных AST bodies тестов не найдено. Закомментированных мёртвых блоков кода
  для безопасного удаления не найдено.
- **Документы не удалялись.** Повторное описание API/coverage из README и E2E
  заменено ссылками на `frontend-api.md`, `frontend-history-requests.md`,
  `frontend-final-features.md` и `graphql-error-details.md`. Дублирующий заключительный
  раздел этапа 5 из frontend-api объединён с существующим специализированным
  контрактом. Уникальные Python capture/Playwright/runner инструкции сохранены.
- Исправлены E2E auth/run-ID/404/AST/error-details инструкции и ключ в server-side
  curl. В deploy docs исправлены legacy routing и назначение API key, удалена
  устаревшая checkout-ветка; `ssh-keyscan <VPS_IP>` заменён синтаксически корректным
  примером с обязательной переменной. Значения конфигурации и secrets не менялись.
- Добавлен `docs/README.md`: единый указатель контрактов, архитектуры и истории.
  Девять исторических документов и четыре release notes получили явную пометку;
  их содержимое, SHA/checksum и команды сохранены. Разные исторические условия
  управления autodeploy не превращены в новую policy.

## Проверки до и после

Одна среда: Python 3.12.3, graphql-core 3.3.2, PostgreSQL 16.15.
Изолированный кластер `/tmp/repo-audit-pg`, только loopback:55449,
разные `audit_app` и `audit_test`; fixtures создают дополнительные временные БД.
Тесты запускались вне сетевых ограничений sandbox. Первый sandbox unit-запуск
завис на asyncio и был остановлен; он не засчитан как успешная проверка.

| Проверка | До | После |
|---|---|---|
| `python -m pytest -m "not integration" -q` | 464 passed, 255 deselected, 0 skipped; 60.30 с | 464 passed, 255 deselected, 0 skipped; 62.38 с |
| `python -m pytest -m integration -q` | 255 passed, 464 deselected, 0 skipped; 388.55 с | 255 passed, 464 deselected, 0 skipped; 299.30 с |
| `python -m pip check` | Та же среда/зависимости | No broken requirements found |
| `python -m compileall -q app` | Исходный Python разбирается AST | passed |
| `git diff --check` | Чистый исходный checkout | passed |
| Alembic | Обе свежие БД upgrade head | 8 revisions, один head, current=head; check: No new upgrade operations detected |
| Ссылки Markdown | 91 ссылка, отсутствующих локальных путей/anchors нет | 293 ссылки, 0 отсутствующих локальных путей/anchors; 15 внешних URL HTTP 200 |
| Bash/Compose | Конфигурация и shell flow проверены при инвентаризации | deploy script и все Bash-блоки Markdown: bash -n; dev/production/auth-smoke Compose config: passed |

Совокупность тестов **719**, без исключения security/regression cases.
`deselected` — другая половина набора при выборе маркера, это не skip.
В обоих полных наборах остаётся прежний Starlette/httpx deprecation warning.
Четыре условных skip-ветви требуют TEST_DATABASE_URL; при проверках он задан.

Сравнение всех **88 Python-файлов** подтвердило: исполняемый AST `app/` совпадает
после исключения docstrings; тела тестов, decorators и assertions совпадают после
исключения импортов/docstrings. **OpenAPI JSON полностью совпадает с исходным.**
Все migration/dependency/workflow/Docker/Compose/Nginx/deploy-script bytes неизменны.
SHA-256 deploy script остаётся
`bd83ef8ce231621556fc2a5205464930a98924c3265777e2bdd5007f3a3f304d`.

Ссылки на внешние документы, оставшиеся в итоговом Markdown, проверены read-only
HEAD (HTTP 200); локальные пути и Markdown anchors проверены отдельно. В исходном
README два production URL `/docs` и `/openapi.json` дали timeout — это не проверка
работоспособности production. В новом README используются локальные пути API;
состояние production не утверждается. Фрагменты внешних страниц не проверялись
по DOM; проверены URL страниц. Исторический GitHub PR #22 сохранён как источник.

Локальный Docker/Nginx runtime smoke не подменяется `compose config`; он должен
пройти в Backend CI на новом HEAD. Финальные SHA, CI и ссылка на этот отчёт
добавляются в русскоязычное описание/комментарий PR после push.

## Отдельные задачи и ограничения

1. **Совместимость graphql-core 3.2.14.** Повторно воспроизведён TypeError для
   `type Query { f: In } input In { x: String }` в `parse_sdl`. Через handler это
   может стать HTTP 500. Известно до PR #31; исправление классификации ошибок и
   compatibility tests требует отдельной задачи. Broad dependency ranges сохранены.
2. **Организация fixtures.** DB/session-factory fixtures импортируются между
   тестовыми модулями. Объединение возможно, но требует отдельного контроля
   isolation, rollback, locking и cancellation; здесь импорты fixtures сохранены.
3. **Python compatibility helpers.** У `change_service_visibility()` и `list_runs()`
   нет прямых in-repo вызовов. Они оставлены: отсутствие внешних Python callers
   не доказано, удаление не является безопасной очисткой публичного интерфейса.
4. **Связность и размер модулей.** `services`/`journal` связаны локальными импортами,
   `api.py`/`services.py` объединяют несколько use cases, сквозные DB-тесты велики.
   Их разделение не входит в этот cleanup и не оправдывает удаление assertions.
5. **Release.** Исторические auth и reliability инструкции исходят из разных
   состояний autodeploy. Для нового выпуска оператор должен выбрать применимую
   процедуру, подтвердить runtime settings, pending migrations/backup и CI.
   Никакие secrets, Variables, production migrations или deployment здесь не менялись.

## Изменения по каталогам

| Раздел | Проверено исходных | Изменено существующих | Добавлено | Суть |
|---|---:|---:|---:|---|
| Корень | 8 | 2 | 0 | README и повтор ignore pattern |
| `app/` | 33 | 6 | 0 | Только комментарии/docstrings |
| `tests/` | 46 | 9 | 0 | 14 imports в 8 модулях, docstrings/conftest comments |
| `alembic/` | 10 | 0 | 0 | Исторические revisions сохранены |
| `docs/` | 20 | 18 | 2 | Контракты, дедупликация, исторический статус, указатель и аудит |
| `deploy/` | 11 | 7 | 0 | Документация и один комментарий env example |
| `.github/` | 1 | 0 | 0 | CI/CD сохранён |
| **Итого** | **129** | **42** | **2** | **44 изменённых/добавленных файла** |

## Полный перечень файлов

Все строки ниже означают «проверен». «Сохранён» — изменения не требуются в рамках
безопасной очистки. «История» — сохраняется исходное содержимое миграции/решения;
для документа мог быть добавлен только вводный статус. База каждой строки —
исходный HEAD выше, новые файлы отмечены отдельно.

| Файл | Результат | Что проверено / решение |
|---|---|---|
| [.dockerignore](../.dockerignore) | Сохранён | Назначение, defaults/build/ссылки; изменений не требуется |
| [.env.example](../.env.example) | Сохранён | Env names/defaults/dev restrictions сверены с реализацией; сохранён |
| [.github/workflows/backend-ci.yml](../.github/workflows/backend-ci.yml) | Сохранён | PostgreSQL/migrations/tests/smoke; deploy только main + explicit gate; сохранён |
| [.gitignore](../.gitignore) | Изменён | Удалён повтор *.egg-info/; правила игнорирования эквивалентны |
| [Dockerfile](../Dockerfile) | Сохранён | Назначение, defaults/build/ссылки; изменений не требуется |
| [README.md](../README.md) | Изменён | Переписан по текущей реализации, сокращён, ссылки на контракты |
| [alembic.ini](../alembic.ini) | Сохранён | Назначение, defaults/build/ссылки; изменений не требуется |
| [alembic/env.py](../alembic/env.py) | Сохранён | Migration environment/template, ORM registration, async/offline config |
| [alembic/script.py.mako](../alembic/script.py.mako) | Сохранён | Migration environment/template, ORM registration, async/offline config |
| [alembic/versions/20261113_initial.py](../alembic/versions/20261113_initial.py) | Сохранён — история | Revision/down_revision, DDL/backfill/constraints/downgrade; байты сохранены |
| [alembic/versions/20261114_service_visibility.py](../alembic/versions/20261114_service_visibility.py) | Сохранён — история | Revision/down_revision, DDL/backfill/constraints/downgrade; байты сохранены |
| [alembic/versions/20261115_browser_auth.py](../alembic/versions/20261115_browser_auth.py) | Сохранён — история | Revision/down_revision, DDL/backfill/constraints/downgrade; байты сохранены |
| [alembic/versions/20261116_service_endpoints.py](../alembic/versions/20261116_service_endpoints.py) | Сохранён — история | Revision/down_revision, DDL/backfill/constraints/downgrade; байты сохранены |
| [alembic/versions/20261117_history_journal.py](../alembic/versions/20261117_history_journal.py) | Сохранён — история | Revision/down_revision, DDL/backfill/constraints/downgrade; байты сохранены |
| [alembic/versions/20261118_final_features.py](../alembic/versions/20261118_final_features.py) | Сохранён — история | Revision/down_revision, DDL/backfill/constraints/downgrade; байты сохранены |
| [alembic/versions/20261119_operation_name_text.py](../alembic/versions/20261119_operation_name_text.py) | Сохранён — история | Revision/down_revision, DDL/backfill/constraints/downgrade; байты сохранены |
| [alembic/versions/20261120_graphql_error_details.py](../alembic/versions/20261120_graphql_error_details.py) | Сохранён — история | Revision/down_revision, DDL/backfill/constraints/downgrade; байты сохранены |
| [app/__init__.py](../app/__init__.py) | Сохранён | Назначение package; изменений не требуется |
| [app/access.py](../app/access.py) | Изменён | CLI key, роли/CSRF; сокращён вводный блок |
| [app/analyzer_adapter.py](../app/analyzer_adapter.py) | Сохранён | Worker thread и bounded schema cache; сохранён |
| [app/api.py](../app/api.py) | Сохранён | Endpoints, ACL, short sessions, safe errors; сохранён |
| [app/api_models.py](../app/api_models.py) | Сохранён | CamelCase DTO, nullable/default/limits; сохранён |
| [app/auth.py](../app/auth.py) | Сохранён | Session hashes, Origin/CSRF, password KDF, throttle и locks |
| [app/auth_api.py](../app/auth_api.py) | Сохранён | Login/logout, recheck роли, последний admin, grants |
| [app/auth_boundary.py](../app/auth_boundary.py) | Сохранён | Body limits, root_path, private cache headers |
| [app/auth_cli.py](../app/auth_cli.py) | Сохранён | TTY/getpass, bootstrap/cleanup, безопасная ошибка |
| [app/auth_contracts.py](../app/auth_contracts.py) | Сохранён | SecretStr, роли, input validation |
| [app/auth_models.py](../app/auth_models.py) | Сохранён | Auth FK/CHECK/индексы, registration side effects |
| [app/config.py](../app/config.py) | Сохранён | Реальные env defaults и отсутствие автоматической загрузки .env |
| [app/contracts.py](../app/contracts.py) | Изменён | Контракты capture/analyzer; сокращены docstrings |
| [app/database.py](../app/database.py) | Сохранён | Session/factory ownership и metadata naming |
| [app/diagnostics.py](../app/diagnostics.py) | Сохранён | Best effort queue, silent sink failure, shutdown и privacy |
| [app/e2e_capture.py](../app/e2e_capture.py) | Сохранён | Разбор envelope, optional metadata, cancellation и запись после upstream |
| [app/graphql_errors.py](../app/graphql_errors.py) | Сохранён | Allowlist, typed paths, bounds, unknown/truncated semantics |
| [app/journal.py](../app/journal.py) | Сохранён | SQL filters/count/pagination, snapshot scope, protected detail |
| [app/main.py](../app/main.py) | Изменён | Middleware/lifespan/validation; удалён очевидный комментарий |
| [app/models.py](../app/models.py) | Сохранён | ORM constraints, operation identity hook, auth registration |
| [app/operation_identity.py](../app/operation_identity.py) | Сохранён | Стабильная JSON/hash формула |
| [app/proxy.py](../app/proxy.py) | Изменён | Wire bytes, headers/cookies, preflight release, no mutation retry |
| [app/query_analyzer.py](../app/query_analyzer.py) | Изменён | Selection/directives/unsupported и privacy; уточнён docstring |
| [app/run_analytics.py](../app/run_analytics.py) | Сохранён | Coverage predicate, counts без JOIN-дублей, UTC/P95 |
| [app/safe_document.py](../app/safe_document.py) | Сохранён | Fail-closed AST mask, literals/defaults, budgets и privacy |
| [app/schema_cache.py](../app/schema_cache.py) | Сохранён | Один immutable LRU cache с пределом 32 |
| [app/schema_collector.py](../app/schema_collector.py) | Сохранён | Pinned transport, no credentials, size/time/CPU budgets |
| [app/schema_metadata.py](../app/schema_metadata.py) | Сохранён | Masked defaults, version compatibility, re-export сохранён |
| [app/schema_parser.py](../app/schema_parser.py) | Сохранён | SDL validation, canonical hash, пределы; известный 3.2 TypeError отдельно |
| [app/schema_work.py](../app/schema_work.py) | Сохранён | Cancellation-safe semaphore lifetime и отсутствие ORM в threads |
| [app/server.py](../app/server.py) | Сохранён | Точные forwarded peer IP, config validation до startup |
| [app/services.py](../app/services.py) | Изменён | Transactions/savepoints/locks/coverage; сокращены исторические вводные |
| [app/upstream.py](../app/upstream.py) | Сохранён | URL/DNS/SSRF, numeric IP pinning, TLS identity, no retries и capacity |
| [compose.yaml](../compose.yaml) | Сохранён | Назначение, defaults/build/ссылки; изменений не требуется |
| [deploy/.env.example](../deploy/.env.example) | Изменён | Актуализированы routing/auth/инструкции; runtime настройки не менялись |
| [deploy/AUTODEPLOY.md](../deploy/AUTODEPLOY.md) | Изменён | Актуализированы routing/auth/инструкции; runtime настройки не менялись |
| [deploy/PR23-RELEASE.md](../deploy/PR23-RELEASE.md) | Изменён — история | Только пометка применимости; migration/rollback/SHA/checksum данные сохранены |
| [deploy/README.md](../deploy/README.md) | Изменён | Актуализированы routing/auth/инструкции; runtime настройки не менялись |
| [deploy/RELIABILITY-RELEASE.md](../deploy/RELIABILITY-RELEASE.md) | Изменён — история | Только пометка применимости; migration/rollback/SHA/checksum данные сохранены |
| [deploy/STAGE2-RELEASE.md](../deploy/STAGE2-RELEASE.md) | Изменён — история | Только пометка применимости; migration/rollback/SHA/checksum данные сохранены |
| [deploy/STAGE5-RELEASE.md](../deploy/STAGE5-RELEASE.md) | Изменён — история | Только пометка применимости; migration/rollback/SHA/checksum данные сохранены |
| [deploy/compose.auth-smoke.yaml](../deploy/compose.auth-smoke.yaml) | Сохранён | Compose/Nginx/shell security, loopback/DB volume/retry/locks; байты сохранены |
| [deploy/compose.production.yaml](../deploy/compose.production.yaml) | Сохранён | Compose/Nginx/shell security, loopback/DB volume/retry/locks; байты сохранены |
| [deploy/nginx/coverage-proxy.conf](../deploy/nginx/coverage-proxy.conf) | Сохранён | Compose/Nginx/shell security, loopback/DB volume/retry/locks; байты сохранены |
| [deploy/vps/graphql-coverage-deploy.sh](../deploy/vps/graphql-coverage-deploy.sh) | Сохранён | Compose/Nginx/shell security, loopback/DB volume/retry/locks; байты сохранены |
| [docs/backend-3-day1.md](../docs/backend-3-day1.md) | Изменён — история | Сохранены уникальные решения/результаты; добавлен исторический статус |
| [docs/e2e-integration.md](../docs/e2e-integration.md) | Изменён | Сверен с текущим кодом; исправлены устаревшие утверждения/дубли и ссылки |
| [docs/error-handling-report.md](../docs/error-handling-report.md) | Изменён — история | Сохранены уникальные решения/результаты; добавлен исторический статус |
| [docs/frontend-api.md](../docs/frontend-api.md) | Изменён | Сверен с текущим кодом; исправлены устаревшие утверждения/дубли и ссылки |
| [docs/frontend-auth.md](../docs/frontend-auth.md) | Изменён | Сверен с текущим кодом; исправлены устаревшие утверждения/дубли и ссылки |
| [docs/frontend-final-features.md](../docs/frontend-final-features.md) | Изменён | Сверен с текущим кодом; исправлены устаревшие утверждения/дубли и ссылки |
| [docs/frontend-history-requests.md](../docs/frontend-history-requests.md) | Изменён | Сверен с текущим кодом; исправлены устаревшие утверждения/дубли и ссылки |
| [docs/frontend-introspection.md](../docs/frontend-introspection.md) | Изменён | Сверен с текущим кодом; исправлены устаревшие утверждения/дубли и ссылки |
| [docs/frontend-multi-service.md](../docs/frontend-multi-service.md) | Изменён | Сверен с текущим кодом; исправлены устаревшие утверждения/дубли и ссылки |
| [docs/graphql-error-details.md](../docs/graphql-error-details.md) | Сохранён | Действующий уникальный контракт/пример, ссылки и privacy; изменений не требуется |
| [docs/multi-service-e2e.md](../docs/multi-service-e2e.md) | Сохранён | Действующий уникальный контракт/пример, ссылки и privacy; изменений не требуется |
| [docs/multi-service-security.md](../docs/multi-service-security.md) | Изменён | Сверен с текущим кодом; исправлены устаревшие утверждения/дубли и ссылки |
| [docs/pr23-review-report.md](../docs/pr23-review-report.md) | Изменён — история | Сохранены уникальные решения/результаты; добавлен исторический статус |
| [docs/reliability-fixes-report.md](../docs/reliability-fixes-report.md) | Изменён — история | Сохранены уникальные решения/результаты; добавлен исторический статус |
| [docs/service-introspection.md](../docs/service-introspection.md) | Изменён | Сверен с текущим кодом; исправлены устаревшие утверждения/дубли и ссылки |
| [docs/stage6-audit-report.md](../docs/stage6-audit-report.md) | Изменён — история | Сохранены уникальные решения/результаты; добавлен исторический статус |
| [docs/v2-implementation-plan.md](../docs/v2-implementation-plan.md) | Изменён — история | Сохранены уникальные решения/результаты; добавлен исторический статус |
| [docs/v2-stage1-report.md](../docs/v2-stage1-report.md) | Изменён — история | Сохранены уникальные решения/результаты; добавлен исторический статус |
| [docs/v2-stage2-report.md](../docs/v2-stage2-report.md) | Изменён — история | Сохранены уникальные решения/результаты; добавлен исторический статус |
| [docs/v2-stage3-report.md](../docs/v2-stage3-report.md) | Изменён — история | Сохранены уникальные решения/результаты; добавлен исторический статус |
| [pyproject.toml](../pyproject.toml) | Сохранён | Dependencies/диапазоны/pytest marker; без изменения версий и настроек |
| [tests/conftest.py](../tests/conftest.py) | Изменён | Сокращены комментарии capture; bounded drain/logger isolation сохранены |
| [tests/docker_auth_smoke.py](../tests/docker_auth_smoke.py) | Сохранён | Изолированный TLS/Nginx/Docker/Uvicorn smoke и cleanup; сохранён |
| [tests/test_access_control.py](../tests/test_access_control.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_access_control_postgres.py](../tests/test_access_control_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_analyzer_adapter.py](../tests/test_analyzer_adapter.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_api_contract.py](../tests/test_api_contract.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_auth_migrations_postgres.py](../tests/test_auth_migrations_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_browser_auth.py](../tests/test_browser_auth.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_browser_auth_postgres.py](../tests/test_browser_auth_postgres.py) | Изменён | Удалены неиспользуемые imports; тела/fixtures/assertions сохранены |
| [tests/test_deploy_retries.py](../tests/test_deploy_retries.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_diagnostics_best_effort.py](../tests/test_diagnostics_best_effort.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_diagnostics_shutdown.py](../tests/test_diagnostics_shutdown.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_e2e_capture.py](../tests/test_e2e_capture.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_e2e_capture_postgres.py](../tests/test_e2e_capture_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_error_diagnostics.py](../tests/test_error_diagnostics.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_error_diagnostics_postgres.py](../tests/test_error_diagnostics_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_final_features_postgres.py](../tests/test_final_features_postgres.py) | Изменён | Удалены неиспользуемые imports; тела/fixtures/assertions сохранены |
| [tests/test_graphql_error_details.py](../tests/test_graphql_error_details.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_graphql_error_details_postgres.py](../tests/test_graphql_error_details_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_graphql_proxy.py](../tests/test_graphql_proxy.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_history_journal_postgres.py](../tests/test_history_journal_postgres.py) | Изменён | Удалены неиспользуемые imports; тела/fixtures/assertions сохранены |
| [tests/test_introspection_security.py](../tests/test_introspection_security.py) | Изменён | Удалены неиспользуемые imports; тела/fixtures/assertions сохранены |
| [tests/test_introspection_transactions_postgres.py](../tests/test_introspection_transactions_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_multi_service_postgres.py](../tests/test_multi_service_postgres.py) | Изменён | Удалены неиспользуемые imports; тела/fixtures/assertions сохранены |
| [tests/test_postgres_integration.py](../tests/test_postgres_integration.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_production_proxy.py](../tests/test_production_proxy.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_proxy_cookie_policy.py](../tests/test_proxy_cookie_policy.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_proxy_e2e.py](../tests/test_proxy_e2e.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_proxy_e2e_postgres.py](../tests/test_proxy_e2e_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_proxy_root_path.py](../tests/test_proxy_root_path.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_query_analyzer.py](../tests/test_query_analyzer.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_run_analytics.py](../tests/test_run_analytics.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_run_analytics_postgres.py](../tests/test_run_analytics_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_safe_document.py](../tests/test_safe_document.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_schema_collector.py](../tests/test_schema_collector.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_schema_collector_postgres.py](../tests/test_schema_collector_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_schema_introspection_api.py](../tests/test_schema_introspection_api.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_schema_metadata.py](../tests/test_schema_metadata.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_schema_parser.py](../tests/test_schema_parser.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_schema_work.py](../tests/test_schema_work.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_schema_work_postgres.py](../tests/test_schema_work_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_sdl_errors_postgres.py](../tests/test_sdl_errors_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_service_dns_transactions_postgres.py](../tests/test_service_dns_transactions_postgres.py) | Изменён | Удалены неиспользуемые imports; тела/fixtures/assertions сохранены |
| [tests/test_service_introspection_postgres.py](../tests/test_service_introspection_postgres.py) | Изменён | Удалены неиспользуемые imports; тела/fixtures/assertions сохранены |
| [tests/test_stage6_audit_postgres.py](../tests/test_stage6_audit_postgres.py) | Сохранён | Тела/assertions, fixtures/decorators, privacy/regressions; без удаления тестов |
| [tests/test_upstream_security.py](../tests/test_upstream_security.py) | Изменён | Удалены неиспользуемые imports; тела/fixtures/assertions сохранены |
| [docs/README.md](README.md) | Добавлен и проверен | Указатель всех действующих и исторических материалов |
| [docs/repository-audit.md](repository-audit.md) | Добавлен и проверен | Этот отчёт: исходный инвентарь, решения и результаты |

Финальный HEAD этого cleanup-коммита фиксируется в описании/комментарии PR #31
после commit/push, чтобы не записывать самоссылочный SHA внутрь коммита.
Функциональный коммит `346ed4c` остаётся отдельным: при независимом ревью можно
сначала проверить его, затем отдельный cleanup diff. Предыдущие отзывы Sol/Astra
относятся к прежнему HEAD; новый HEAD требует нового независимого ревью.
