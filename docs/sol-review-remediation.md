# Sol review remediation — Backend + QA

Дата: 18 ноября 2026. Изменения готовы для повторного независимого ревью Sol.
Merge и deployment не выполнялись. GitLab-магазин не изменялся.

## Исходные версии и ветки

- Backend: `aa6aa76df58059f8dec16a635542c4fecd129156`, актуальный main при начале работы.
  [Ветка исправлений](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-Beckend/tree/codex/sol-execution-run-fixes).
- QA: `bd8ae646ea82e1303e7b05eacf36da715bd4939a`, ветка `feat/forma-e2e-test-coverage`.
  [Ветка исправлений](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-QA/tree/codex/sol-qa-safety-fixes).
- Рабочие копии изолированы в `/tmp/coverage-sol-backend` и `/tmp/coverage-sol-qa`.
  Исходный рабочий каталог пользователя, его ветка и незакоммиченные файлы не изменялись.

## Все семь замечаний

| ID | Статус | Подтверждение первопричины и решение |
| --- | --- | --- |
| B1 P2 | Подтверждено, исправлено | Fixture записывала annotation после подтверждения POST. При потере всех ответов reporter создавал новый UUID и получал 409. Annotation с исходными UUID и metadata теперь появляется до отправки; reporter повторяет исходную регистрацию, затем неизменяемый finish. Повторы чтения потерянного JSON-ответа также идемпотентны. Исправлены обе копии SDK. |
| B2 P2 | Подтверждено, исправлено | Допуск учитывался только у execution; finish_run не проверял активный upstream exchange. У run теперь свой счётчик для всех запросов. Допуск и /finish сериализуются блокировкой строки run; соединение отпускается до upstream. Очистка защищена от отмены, в том числе сразу после commit. GitLab monitor сохраняет terminal outcome и повторяет завершение после drain. |
| Q1 P1 | Подтверждено, исправлено | Каждый новый pytest-процесс начинал attempt с 1. Добавлен opt-in `attempt: null`: Backend назначает max+1 под той же run-блокировкой. UUID остаётся прежним при повторе одного POST; явные занятые attempts по-прежнему дают 409. QA использует распределение Backend; новый запуск панели сохраняет отдельный отчёт, pytest retries — отдельные executions. Неподтверждённые результаты прошлых retries сохраняются для shutdown recovery. |
| Q2 P1 | Подтверждено, исправлено | Группа e2e включала создание/отмену заказов и fault injection. Введён точный server allowlist: только `tests/e2e/test_shop.py::test_storefront_catalog_and_cart`. Fixture в deployed mode больше не выполняет login mutation. Панель и pytest после обработки selection.json проверяют каждый ID. Isolated commerce тесты с mutations и очисткой сохранены. |
| Q3 P1 | Подтверждено, исправлено | QA HTTP endpoints передавали произвольный host/URL в HTTP-клиент и возвращали исходные исключения. Избыточные trigger/status/gate HTTP endpoints теперь отвечают 410 без сетевого доступа; формы ввода токенов удалены. Pipeline lifecycle остаётся в Backend. CLI GitLab client ограничен https://gitlab.com, запрещает credentials/path/query/fragment/другие hosts и redirects, валидирует pipeline ID. Исходные ошибки trigger/status/poll не печатаются. CLI artifact gate и отчёты сохранены. |
| Q4 P2 | Подтверждено, исправлено | CoverageRun.graphql/snapshot читали первую общую страницу limit=500. Теперь все request pages ограничены testExecutionId и читаются до конца в стабильном порядке. Проверки общего FORMA run сравнивают данные executions и допускают чужие обращения; run-level граф/operation aggregates сохраняют явную общую область. |
| Q5 P2 | Подтверждено, исправлено | CLI проверял наличие параметров, но не активность и сервис run. Общий модуль `coverage_qa.deployed` используется панелью, launcher и pytest. Проверяются canonical UUID, сервис forma-demo-shop, status=running и совпадение CLI targets с серверной конфигурацией. Невалидный/чужой/завершённый run и подмена selection.json отклоняются до тестовых операций. |

Часть B1 уже работала на исходном Backend: регистрация по UUID и finish были
идемпотентными. Это доказывает существующий PostgreSQL-тест
`test_registration_result_retry_and_attempt_identity_are_idempotent`; он проходит.
Серверную идемпотентность B1 не переписывали. Изменение create_execution относится
к Q1 и сохраняет проверку конфликтующих metadata и явных attempts.

## Подтверждение на исходных SHA

- Новая B2 concurrency-регрессия на исходном Backend получила преждевременный 200
  от /finish при медленном upstream (с executionId и без него).
- Playwright-regression на исходном QA: после трёх потерянных ответов fixture
  reporter создаёт другой UUID; восстановление результата завершается failed/409.
- Исходные Backend и QA вместе: второй реальный дочерний pytest-процесс того же
  testId в общем run получает HTTP 409. В исправленной версии последовательные
  запуски получают 1,2,3, параллельные — 4,5,6,7; явный повтор attempt=2 отклонён.
- Исходный CoverageRun.snapshot на наборе 1003 собственных + 507 чужих обращений
  возвращает общую первую страницу 500. Исправленный возвращает 1003 собственных.
- Исходный QA не отклоняет недоверенные GitLab hosts; серверный выбор destructive
  e2e и malformed deployed-run CLI не соответствуют новым отрицательным тестам.
- Исторические тесты и red-логи сохранены отдельно от рабочих копий.

## Проверки

| Проверка | Результат |
| --- | --- |
| Backend полный набор, Python 3.12, Node 22.23.5, PostgreSQL 18.6 | 1045 passed |
| Backend полный набор, PostgreSQL 16.16 | 1045 passed |
| QA platform без подмены инфраструктуры | 121 passed |
| QA tests/api + tests/e2e, изолированные PostgreSQL/commerce/HTTP/Chromium | 111 passed, 1 skipped |
| Ключевые QA shared-run/Playwright regressions на PostgreSQL 16 | 5 passed |
| Node SDK unit tests Backend | 3 passed |
| Node SDK unit tests QA | 3 passed |
| Неизменённые GitLab shop tests на локальном HTTPS | 6 passed |
| Неизменённый GitLab coverage gate | PASS 88.57%: 31/35; backend-managed finish проверен |
| pip check; compileall; node --check; git diff --check | passed |

Пропущен TST-07 основного Coverage frontend: отсутствует соседний production
build `dist/index.html`. Внешние live GET-проверки не включались. Проверка вкладки
Test Coverage самого TestLab выполнена браузером в FORMA lifecycle-регрессии.

Первый полный QA-прогон временного native PostgreSQL harness дал 228 passed,
5 skipped и один сбой test_missing_backend_is_actionable: harness не повторил
штатную проверку отсутствующего checkout. Проверка добавлена только в временный
harness; продуктовый код не меняли. Итоговые platform и integration наборы
выше выполнены раздельно и проходят. Docker daemon в этой сессии недоступен.
PostgreSQL 16 распакован из пакета в /tmp без системной установки.

### Команды

Backend (из checkout, зависимости `.[test]` и `integrations/python` установлены):

```sh
npm ci --prefix integrations/playwright
DATABASE_URL="$LOCAL_TEST_DATABASE_URL" alembic upgrade head
TEST_DATABASE_URL="$LOCAL_TEST_DATABASE_URL" pytest -q
node --test integrations/playwright/index.test.cjs
```

В проверенной сессии LOCAL_TEST_DATABASE_URL указывал только на отдельные
loopback PostgreSQL: порт 55438 для 18, порт 55439 для 16; пользователь alexei,
база postgres. Никакие production DSN не использовались.

QA (обычный воспроизводимый запуск при доступном Docker):

```sh
pytest -q tests/platform
pytest -q -rs --managed-coverage --coverage-backend-dir=/path/to/backend tests/api tests/e2e
node --test integrations/playwright/index.test.cjs
```

Фактический fallback этой сессии для integration:

```sh
PYTHONPATH=/tmp:/tmp/coverage-sol-qa/src \
PLAYWRIGHT_BROWSERS_PATH=/home/alexei/.cache/ms-playwright \
pytest -q -rs -p coverage_sol_local --managed-coverage \
  --coverage-backend-dir=/tmp/coverage-sol-backend tests/api tests/e2e
```

`coverage_sol_local.py` — временный harness: отдельные БД, штатные миграции,
настоящие HTTP-сервисы и Chromium; он заменяет только запуск Docker инфраструктуры.
Для PostgreSQL 16 отдельно запускались test_shared_execution_regressions.py и
оба варианта test_forma_e2e_coverage.py. Тестовые исходники и логи находятся
в локальном пакете evidence, не добавлены в репозитории.

## GitLab-managed контракт

Через GitLab connector прочитаны pipeline 3084114321 и его jobs: quality,
shop-smoke и coverage-e2e — success. Источник магазина:
`2bd42d5bacafe962e30da92a4145d7474c682f37`.

Из него без изменений выполнены tests/coverage_client.py,
tests/test_graphql_e2e.py и tests/coverage_gate.py против отдельной локальной
commerce БД и исправленного Backend через HTTPS. Использовались cookie login,
service grant, CSRF и runId без testExecutionId, как в рабочем CI. Gate снова
получил 88.57% (31/35). Run оставался running во время gate, ручной finish managed
run отклонялся; terminal pipeline outcome затем закрывал его через monitor.
Реальные запросы к публичному магазину не выполнялись, новый GitLab pipeline не
запускался, репозиторий магазина не менялся.

## Коммиты

### Backend

- [b687b5c](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-Beckend/commit/b687b5cbf39860a90de23543e05840fa8c5687e3) fix(playwright): recover original execution after lost registration responses
- [bd38354](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-Beckend/commit/bd3835425ec37eb7a981834d49ab81450810b41f) fix(proxy): keep runs open until all admitted GraphQL exchanges finish
- [148187f](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-Beckend/commit/148187f2ec28123fdf8f02d562f435729ca84ac6) feat(tests): allocate shared-run attempts atomically with idempotent UUID recovery

### QA

- [df060b6](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-QA/commit/df060b647b89834b5bb29c3c4b7a1b1ccfbdb868) fix(playwright): retain registration identity for reporter recovery
- [2a96c3f](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-QA/commit/2a96c3f2a9e00c79b5824b236a22a68b66394745) fix(security): retire QA GitLab HTTP proxy and restrict CLI destinations
- [87debca](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-QA/commit/87debcae88a64acb83c5d974030f226c92ecda85) fix(tracking): allocate attempts and preserve results across launches and retries
- [51d15c3](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-QA/commit/51d15c36a799df640f180ace54725d9a1b5d830e) fix(forma): enforce shared deployed-run validation and exact safe scenario allowlist
- [3c84d24](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-QA/commit/3c84d2459d7a5f4383d3108c6c554a5022bf916e) fix(reporting): page execution-scoped requests and cover shared-run security regressions
- [17c0417](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-QA/commit/17c0417ef6a6c435a5dd5ad5f6e99d944e707ba3) ci: pin compatible Backend for atomic test attempt allocation
- [d2ae644](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-QA/commit/d2ae644717139066ff0df9f59c6bbde87b073611) fix(forma): pass normalized deployed run identity to pytest

## Изменённые файлы

### Backend

- `alembic/versions/20261125_run_in_flight.py`
- `app/gitlab_runs.py`
- `app/models.py`
- `app/proxy.py`
- `app/services.py`
- `app/test_executions.py`
- `docs/sol-review-remediation.md`
- `docs/test-coverage.md`
- `integrations/playwright/index.cjs`
- `integrations/playwright/index.test.cjs`
- `tests/test_auth_migrations_postgres.py`
- `tests/test_execution_attempt_allocation_postgres.py`
- `tests/test_gitlab_runs_postgres.py`
- `tests/test_proxy_e2e.py`
- `tests/test_proxy_e2e_postgres.py`
- `tests/test_sol_regressions_postgres.py`
- `tests/test_stage6_audit_postgres.py`
- `tests/test_test_coverage_postgres.py`
- `tests/test_test_coverage_runners_postgres.py`

### QA

- `.github/workflows/tests.yml`
- `.gitlab-ci.yml`
- `CONTRIBUTING.md`
- `docs/forma-shop-vps.md`
- `docs/gitlab-ci.md`
- `docs/sol-review-remediation.md`
- `integrations/playwright/index.cjs`
- `integrations/playwright/index.test.cjs`
- `src/coverage_qa/control_panel.py`
- `src/coverage_qa/deployed.py`
- `src/coverage_qa/gitlab_ci.py`
- `src/coverage_qa/pytest_plugin.py`
- `src/coverage_qa/runner.py`
- `src/coverage_qa/tracking.py`
- `src/coverage_qa/web/app.js`
- `src/coverage_qa/web/index.html`
- `tests/api/coverage/test_forma_e2e_coverage.py`
- `tests/api/coverage/test_shared_execution_regressions.py`
- `tests/conftest.py`
- `tests/platform/test_deployed_forma.py`
- `tests/platform/test_gitlab_ci.py`
- `tests/platform/test_sol_safety.py`
- `tests/platform/test_storefront_tracking.py`
- `tests/platform/test_tracking.py`
- `tests/support/coverage_client.py`
- `tests/support/storefront.py`

## Согласованное обновление

1. Сначала установить Backend с opt-in attempt:null и миграцией
   20261125_run_in_flight; только затем новый QA tracking. Старые явные attempts
   и форматы ответов/отчётов сохранены. QA GitHub и GitLab CI закрепляют совместимый
   Backend code SHA 148187f2ec28123fdf8f02d562f435729ca84ac6.
2. Обновить обе копии Playwright SDK. Исправление B1 само по себе совместимо со
   старым API, поскольку регистрация и finish по UUID уже идемпотентны.
3. Перевести пользователей старых QA GitLab HTTP endpoints на Backend-managed
   запуск либо доверенный CLI artifact gate: ответы 410 — намеренное ограничение.
   Действующий GitLab-магазин эти endpoints не использует.

## Риски и ручные проверки для Sol

- Перед миграцией остановить новый трафик и дождаться завершения запросов старых
  workers. Одновременная работа старого и нового admission-кода небезопасна.
- SIGKILL/потеря процесса proxy или неопределённый сбой commit очистки могут
  оставить ненулевой durable счётчик. Run остаётся открытым (fail closed).
  Автоматическое обнуление по TTL не добавлено: оно могло бы закрыть run при
  живой mutation. Для восстановления оператор должен остановить трафик и
  подтвердить отсутствие живых workers/обращений.
- Нужен TST-07 с production build основного frontend и штатный контейнерный CI.
  Ручной dispatch Actions из этой сессии недоступен: gh не авторизован. Локальная
  проверка PostgreSQL 16 выполнена отдельно; отсутствие remote CI статуса не
  подменяется локальным результатом.
- Деплой после ревью требует проверить серверные QA_DEPLOYED_* настройки и
  COVERAGE_API_KEY, применить миграцию и повторить безопасный read-only smoke.
  Destructive tests выполнялись исключительно в fixture-owned локальных БД.
- Предел 10000 attempts остаётся явным конфликтом 409. После его достижения
  оператор создаёт новый run; ошибка не подавляется.
- Deployed allowlist намеренно минимален. Новые сценарии допускаются только после
  проверки самих тестов и всех их fixtures; принадлежность группе e2e недостаточна.

Работа остановлена для повторного независимого ревью Sol. Merge, deployment и
повторный запуск production pipeline требуют отдельной команды пользователя.
