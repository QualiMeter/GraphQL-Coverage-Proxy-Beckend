# Service-aware introspection: устройство и ограничения

Этап 3, база main 09659aed51f91dd6e93d95a3818497901c96349c. Миграции не нужны:
используются SchemaSnapshot, SchemaType, SchemaField и существующий UNIQUE
(service_id, schema_hash). Coverage Engine, /graphql и модель RBAC не меняются.

## Путь запроса

POST /api/schemas/import/introspection проверяет operator/admin и cookie/CSRF
либо доверенный CLI API key. Для operator требуется grant, даже если сервис public.
После проверки доступа сервисный слой получает Service и его endpoint_url.
Произвольные URL/headers/credentials через API не принимаются. Настроенный endpoint
имеет приоритет всегда; для старого NULL endpoint допускается только explicit
COVERAGE_LEGACY_PROXY=true + TARGET_GRAPHQL_URL. Без настройки — 409.

Collector выполняет обычный get_introspection_query() graphql-core. Используются
validate_endpoint и PinnedTransport этапа 2: все DNS IP проверяются непосредственно
перед HTTP, сокет подключается к проверенному числовому IP и проверяется peer,
TLS SNI и certificate hostname сохраняют исходное имя. HTTPS:443 production,
локальный HTTP — только explicit dev + точный origin allowlist. Нет redirects,
автоматических retries или environment proxies (trust_env=False).

Не передаются входящие Authorization, Cookie, CSRF, API key и coverage headers.
Клиент/pool/cookie jar создаётся на один обмен. Upstream Set-Cookie не выдаётся
frontend и не используется в последующих запросах. Сохраняется только схема:
introspection_json = {data:{__schema:...}}, без errors, extensions и посторонних
полей ответа; в API возвращается SchemaOut без URL, upstream тела и credentials.
Нормализованные SDL и hash вычисляются прежним parse_sdl(print_schema(...)).

## Ограничения ресурсов и ошибок

Тело входа — 16 КиБ. Ответ — 8 МиБ raw, только identity encoding; запрос просит
Accept-Encoding: identity. Общий timeout DNS+HTTP fetch — 15 секунд, DNS отдельно
до 3 секунд. Общий с /graphql capacity limit — 16 jobs/worker, без очереди;
слот удерживается до завершения нормализации. JSON и schema parsing вынесены
в worker thread; не вводится фиктивный timeout, который оставил бы неограниченно
работающие фоновые потоки. CPU-нормализация имеет структурные бюджеты:
200000 JSON nodes, глубина 64, 5000 типов, 50000 полей, имена до 255 символов.
Это конечные бюджеты сложности, а не гарантия wall-clock deadline CPU-парсинга.
Поток закрывается при timeout/превышении размера. Никаких повторных HTTP запросов.

Доменная ошибка: прежний строковый detail + новый code. Таблица HTTP/code:
[frontend-introspection.md](frontend-introspection.md). DNS failure — 502,
DNS/connect/read/total fetch timeout — 504, forbidden destination — 409.
HTTP 3xx возвращает безопасный 502, Location не выдаётся и не запрашивается.
Невалидная схема/JSON, GraphQL errors и превышение лимитов ничего не сохраняют.
SQLAlchemy failures дают безопасный 503 без SQL/DSN; unique conflict — 409.

Явный запрет introspection распознаётся по extensions.code=INTROSPECTION_DISABLED
или известным формулировкам GraphQL.js/Apollo. GraphQL.js содержит отдельное
[опциональное правило отключения introspection](https://github.com/graphql/graphql-js/blob/16.x.x/src/validation/rules/custom/NoSchemaIntrospectionCustomRule.ts).
Распознавание сообщений ограничено: неизвестная формулировка может остаться
UPSTREAM_GRAPHQL_ERRORS/UPSTREAM_HTTP_ERROR. HTTP 401/403 сам по себе не означает
отключённую introspection. Для всех таких случаев UI может предложить ручной SDL.
Текст upstream ошибки не возвращается и не пишется в persistent logs.

## Атомарность и конкурентность

HTTP и проверка схемы происходят до service row FOR UPDATE. Блокировка берётся
только для сохранения; её используют SDL и introspection. После lock повторно
проверяется captured endpoint: при изменении — 409 SERVICE_ENDPOINT_CHANGED,
без записи схемы со старого адреса. Concurrent endpoint edit не требует держать
DB lock во время сетевого ожидания и не блокирует /graphql.

Весь набор snapshot/type/field, включая первый flush, находится в savepoint.
При ошибке откатывается импорт без частичных строк; низкоуровневая функция не
откатывает чужие pending изменения вызывающей session. См. официальную
[семантику SQLAlchemy SAVEPOINT](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#using-savepoint).
Успех завершается commit. API request имеет собственную session и при storage
failure дополнительно делает rollback. Блокировка сохраняется до commit,
поэтому повторный конкурентный импорт видит уже существующий hash и возвращает
тот же snapshot ID. Исторические snapshots/runs/observations не удаляются.

При совпадении с ранее импортированным SDL обновляются только source=introspection
и introspection_json. schema_sdl/hash/id/created_at, типы/поля и их ID сохраняются.
При повторном SDL не обнуляются уже имеющиеся introspection metadata.
Одинаковый hash у разных Service остаётся отдельным снимком каждого сервиса.

## Upstream-аутентификация

В этом PR её нет: закрытый API возвращает безопасную ошибку, применяется ручной SDL.
Нельзя помещать upstream токен в endpoint query/userinfo, description, browser
localStorage, публичный JS или произвольные API headers для пересылки.

Отдельное предлагаемое решение, требующее согласования: секрет хранится в server-side
secret manager, в БД — только ссылка на версионированный credential binding,
жёстко привязанный к одному serviceId/допустимому origin. Создание/назначение binding
только доверенным администратором/CLI с аудитом и ротацией; operator grant сам по
себе не позволяет привязать чужой credential. Collector извлекает секрет в памяти
серверного процесса и добавляет узко разрешённый Authorization для проверенного
endpoint, без redirects, логов и выдачи секрета frontend. Cookie и administrative
Coverage key остаются запрещены. Нужны отдельные threat model, schema/migration,
redaction, права secret-manager и автоматические тесты перед реализацией.

## Проверки

- Два настоящих TCP GraphQL upstream: Service A → схема A, B → схема B; global
  target заведомо неверен, в том числе при enabled legacy.
- PostgreSQL: повторный hash, enrichment SDL snapshot с существующими полями/run/
  observations, два concurrent introspection + SDL import, endpoint edit во время
  fetch, отказ flush новых полей и enrichment с rollback.
- ACL всех ролей, grants на private/public, отсутствие credentials upstream.
- SSRF/DNS rebinding на реальном httpcore transport с подменённой сетью, запрет
  небезопасного socket connect, TLS hostname verification, redirects, timeouts,
  stream limits/close, shared capacity и malformed/disabled introspection.
- Полный существующий CI с PostgreSQL 16 и реальным Nginx/Docker/Uvicorn smoke.
