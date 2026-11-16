# Отчёт: этап 2 v2.0, Multi-service и безопасный proxy

> Исторический документ: результаты, ограничения и планы относятся к указанной
> в нём базе. Действующие контракты — в [указателе документации](README.md).

> Исторический отчёт на момент PR #24. В этапе 3 временный запрет service
> introspection снят: [актуальный контракт](frontend-introspection.md).

База main: bf9cdb2cdbcbdc7d7fd2f4e41b10ea8f910c641a. Ветка codex/v2-multi-service.
HEAD и ссылка CI фиксируются в PR и итоговом сообщении после публикации.

Реализованы nullable endpoint_url, description, updated_at; создание/чтение/
редактирование сервисов с сохранением ролей, ACL, cookie/CSRF; скрытие endpoint
для anonymous/viewer/analyst и operator без grant. Список использует один SQL,
без N+1. Proxy маршрутизирует строго run → snapshot → service, проверяет UUID,
running и grant до upstream. Endpoint update проверяет все snapshots и возвращает
409 при running run. Общая PostgreSQL service lock закрывает гонку с create_run.

Новый transport проверяет все DNS-ответы, подключает проверенный числовой IP и
проверяет peer, сохраняя исходные TLS SNI/hostname/certificate verification.
HTTPS:443 production, HTTP/local только explicit dev origin allowlist, no redirects,
no proxies/retries, bounded request/response/time/concurrency. Browser cookies,
служебные и hop-by-hop headers фильтруются; jar и pool только на один обмен.
Сбой записи после mutation явно сообщает о возможном выполнении, без retry.
Coverage Engine, query_hash/operationKey и семантика наблюдений не менялись.

Endpoints: POST/GET /api/services, новый GET /api/services/{serviceId},
PATCH /api/services/{serviceId}, POST /graphql. Introspection временно 409 для
configured-сервисов до этапа 3; legacy NULL сервисы — только explicit legacy flag
с общей SSRF-защитой. [Точные DTO и права](frontend-multi-service.md).

Миграция 20261116_service_endpoints после 20261115_auth добавляет nullable
endpoint_url/description, updated_at=created_at старым строкам с последующим
NOT NULL/default now. URL не заполняются; исторические снимки, runs, observations
сохраняются. PostgreSQL upgrade/downgrade/upgrade и alembic check проверены на
исторических строках. [Будущий выпуск и rollback](../deploy/STAGE2-RELEASE.md).

Проверки локально, Python 3.12.3 / PostgreSQL 18.6:

- Полный pytest: **313 passed**, 1 существующее Starlette/httpx deprecation warning,
  без skipped; 252 unit и 61 integration. Включены два реальных TCP upstream и
  параллельная запись PostgreSQL, независимые DB соединения для двух lock races,
  ACL, migration history, DNS rebinding/peer mismatch/TLS SNI, SSRF и cookie regression.
- Контрольный test_control_scenario_coverage_through_proxy из открытого PR #22,
  запущенный отдельно поверх ветки с конфигурацией pytest проекта: **1 passed**.
  Его код не добавлен в этот PR.
- pip check: No broken requirements found.
- Docker Compose config (dev/production с фиктивным env), bash -n deploy script,
  git diff --check: exit 0.
- Локальный Docker daemon недоступен; реальный Nginx TLS → Docker → Uvicorn smoke
  выполняется существующим CI PostgreSQL 16. Итог CI проверяется на HEAD PR.

Ограничения: service introspection — следующий этап; HTTP POST, без WebSocket/
потоковой доставки; нет retries по другим IP, первого недоступного IP достаточно
для безопасного отказа. Legacy default false требует миграции старых клиентов;
NULL endpoint нельзя настроить до завершения активных runs. Публичный description
не предназначен для секретов. DNS/global-IP policy не заменяет контроль самого
разрешённого upstream и доверенной сетевой инфраструктуры.

Production не менялся, merge/deploy/autodeploy не выполнялись. Риски будущей
выкладки: DDL lock, переход старых clients/NULL endpoints, ограничения HTTPS/портов/
размеров, осторожный rollback старого глобального proxy. Остальные P0/P1 ещё
предстоит реализовать; всю backend-часть завершённой не объявляем. P2 не начат.

Изменённые файлы этого этапа:

- `.env.example`
- `README.md`
- `app/api.py`
- `app/api_models.py`
- `app/auth_boundary.py`
- `app/main.py`
- `app/models.py`
- `app/proxy.py`
- `app/schema_collector.py`
- `app/services.py`
- `compose.yaml`
- `deploy/.env.example`
- `deploy/compose.production.yaml`
- `docs/e2e-integration.md`
- `docs/frontend-api.md`
- `docs/v2-implementation-plan.md`
- `pyproject.toml`
- `tests/docker_auth_smoke.py`
- `tests/test_access_control.py`
- `tests/test_api_contract.py`
- `tests/test_auth_migrations_postgres.py`
- `tests/test_browser_auth_postgres.py`
- `tests/test_graphql_proxy.py`
- `tests/test_proxy_cookie_policy.py`
- `tests/test_proxy_e2e.py`
- `tests/test_schema_introspection_api.py`
- `alembic/versions/20261116_service_endpoints.py`
- `app/upstream.py`
- `deploy/STAGE2-RELEASE.md`
- `docs/frontend-multi-service.md`
- `docs/multi-service-e2e.md`
- `docs/multi-service-security.md`
- `docs/v2-stage2-report.md`
- `tests/test_multi_service_postgres.py`
- `tests/test_upstream_security.py`
