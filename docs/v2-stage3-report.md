# Этап 3: Service-aware GraphQL Introspection

База main: `09659aed51f91dd6e93d95a3818497901c96349c`, после PR #24.
Ветка: `codex/v2-service-introspection`. HEAD, PR и результат CI фиксируются
в описании PR и итоговом сообщении после публикации.

## Что реализовано

POST /api/schemas/import/introspection получает только serviceId. Сервисный слой
находит сохранённый endpoint_url и выполняет стандартный introspection query
через validate_endpoint/PinnedTransport. Configured Service всегда использует
свой endpoint, даже при включённом legacy и неверном TARGET_GRAPHQL_URL.
NULL endpoint допускает только явно включённый legacy fallback; иначе 409.

Сохранены RBAC/grants/cookie/CSRF: admin — любой сервис, operator — только grant,
viewer/analyst/anonymous не импортируют. Недоступный service и отсутствующий UUID
дают одинаковый безопасный 404. Incoming Cookie/Authorization/API key/CSRF/coverage
headers не отправляются upstream. Произвольные URL/headers/credentials в JSON
отклоняются без отражения секретов в validation response.

Общий pinned transport проверяет все DNS-ответы и фактический socket peer,
сохраняет исходные TLS SNI/hostname verification; HTTPS:443 production,
explicit dev origin allowlist, no redirects/retries/proxies, trust_env=False.
Introspection удерживает общий с proxy capacity slot (16 jobs/worker) до конца
нормализации. Лимиты: request 16 КиБ, raw response 8 МиБ / identity encoding,
DNS до 3 с, DNS+HTTP fetch до 15 с, структурные бюджеты JSON/schema. Нормализация
в worker thread, без неограниченных фоновых задач после фиктивного timeout.

Ошибки сохраняют строковый detail и добавляют code. Явный запрет introspection —
409 INTROSPECTION_DISABLED с рекомендацией ручного SDL. DNS/HTTP/JSON/schema,
size/capacity и storage errors классифицированы без URL, SQL и upstream текста.

## Данные и миграции

Новых миграций нет: существующих таблиц и UNIQUE(service_id,schema_hash) достаточно.
Service row FOR UPDATE сериализует сохранение SDL/introspection. Все flush
нового snapshot/types/fields и enrichment находятся в savepoint; ошибка не
оставляет частичных строк. Concurrent imports возвращают один snapshot.

При совпадении hash с SDL сохраняются snapshot id/hash/SDL/createdAt, IDs типов/
полей, runs и observations. Обогащаются source и introspection_json; сохраняется
только data.__schema, без посторонних response extensions/errors/data.
Endpoint change во время fetch возвращает 409 SERVICE_ENDPOINT_CHANGED без записи
устаревшей схемы. HTTP выполняется без удержания service row lock.
Coverage Engine, /graphql и модель авторизации не изменены.

## Проверки

Локально Python 3.12.3 / PostgreSQL 18.6:

- Полный pytest: **367 passed**, без skipped, 1 существующее предупреждение
  Starlette/httpx deprecation. Включены все прежние auth/multi-service/coverage
  проверки и новые unit/PostgreSQL integration.
- Расширенный сквозной тест: **1 passed** — два настоящих TCP upstream, импорт
  схем A/B, dedup/enrichment с сохранением истории, затем Run → /graphql →
  реальный upstream B → Coverage 2/3 → finish. Выполнен после полного прогона.
- Контрольный тест открытого PR #22: **1 passed**, отдельно поверх ветки,
  без копирования его кода в PR.
- pip check: No broken requirements found.
- alembic check на PostgreSQL: No new upgrade operations detected.
- Compose config, bash -n deploy script, compileall, git diff --check: exit 0.
- Полный существующий CI на PostgreSQL 16, включая настоящий Nginx TLS → Docker
  NAT → Uvicorn smoke, проверяется на HEAD PR; результат — в описании PR.

Тесты подтверждают отсутствие подключения к запрещённым socket addresses,
проверку DNS rebinding/TLS hostname, redirects/timeouts/stream close/size/capacity,
ACL всех ролей, отсутствие credential forwarding, rollback на PostgreSQL CHECK
failure при вставке полей и при enrichment, concurrent SDL + два introspection,
endpoint edit во время fetch, безопасные ошибки без частичных схем.

## API-контракт и ограничения

Изменён только контракт POST /api/schemas/import/introspection: configured Service
поддержан; запрос serviceId и успешный SchemaOut прежние, к доменным ошибкам
добавлен code и уточнены HTTP-коды. Схема ошибок/OpenAPI обновлены. Остальные DTO
и права редактирования сервисов оператором с grant сохранены.
[Frontend delta и таблица ошибок](frontend-introspection.md),
[архитектура и предложение upstream auth](service-introspection.md),
[пример двух сервисов](multi-service-e2e.md).

Upstream аутентификация не реализована: закрытый API требует ручной SDL.
Предложен отдельный secret-manager credential binding с привязкой к service/origin,
правами администратора, аудитом/ротацией; небезопасного хранения токенов нет.
Неизвестный формат сообщения о запрете introspection может остаться generic
GraphQL/HTTP error с предложением SDL. Только identity response encoding;
network fetch имеет wall-clock timeout, CPU parsing — конечные структурные бюджеты.
SDL download/displayName/расширенные schema DTO, история, P1-аналитика, safe AST
и P2 в этот PR не входят.

Production, Nginx, Docker/PostgreSQL на VPS, secrets, autodeploy settings не менялись.
Деплой и merge не выполнялись. Для будущего выпуска нужен прежний безопасный
порядок с отключённым автодеплоем и проверкой deploy=skipped. Нового DDL нет;
остаются риски недоступной/закрытой introspection, лимитов крупных схем и
необходимости ручного SDL. Этапы 1–3 не означают завершения всех P0/P1.

## Изменённые файлы

- `README.md`
- `app/api.py`
- `app/api_models.py`
- `app/auth_boundary.py`
- `app/main.py`
- `app/schema_collector.py`
- `app/services.py`
- `app/upstream.py`
- `deploy/STAGE2-RELEASE.md`
- `docs/e2e-integration.md`
- `docs/frontend-api.md`
- `docs/frontend-multi-service.md`
- `docs/multi-service-e2e.md`
- `docs/multi-service-security.md`
- `docs/v2-implementation-plan.md`
- `docs/v2-stage2-report.md`
- `tests/test_multi_service_postgres.py`
- `tests/test_schema_collector_postgres.py`
- `tests/test_schema_introspection_api.py`
- `docs/frontend-introspection.md`
- `docs/service-introspection.md`
- `docs/v2-stage3-report.md`
- `tests/test_introspection_security.py`
- `tests/test_service_introspection_postgres.py`
