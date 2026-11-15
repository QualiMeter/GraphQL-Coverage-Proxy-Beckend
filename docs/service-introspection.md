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

Introspection получает фабрику новых AsyncSession, а не живую request/caller
session. Cookie authentication тоже читает пользователя в собственной короткой
сессии, закрытой до выполнения endpoint. Политика роли/CSRF остаётся общей.

1. В короткой read-транзакции повторно проверяются аутентификация, operator/admin
   и grant; читаются endpoint и legacy fallback в обычные строковые значения.
   Транзакция и сессия закрываются до DNS/HTTP/нормализации, возвращая connection в pool.
2. collect_schema не имеет AsyncSession/connection и не удерживает PostgreSQL
   checkout во время ожидания upstream. Это относится и к cookie-клиенту, и к CLI key.
3. Сохранение открывает новую короткую сессию и write-транзакцию. После service
   row FOR UPDATE заново читаются пользователь/роль/активность, BrowserSession
   (отзыв/expiry) и service grant, затем сравнивается captured endpoint. Отзыв
   доступа отклоняет импорт до записи и до выдачи сведений об изменённом endpoint.
4. Весь набор snapshot/type/field, включая первый flush, защищён savepoint.
   После успешного write commit обе сессии закрыты. SchemaOut материализуется
   до commit: post-commit refresh/lazy load не открывает лишнюю транзакцию.

При изменении endpoint — 409 SERVICE_ENDPOINT_CHANGED без старой схемы. При
отзыве grant — безопасный 404, смене роли — 403, отзыве/истечении сессии или
блокировке пользователя — 401. Смена endpoint вместе с отзывом grant тоже
возвращает 404. Проверки актуальны на момент write-транзакции; после её commit
обычные ACL-чтения по-прежнему ограничивают доступ к сохранённому snapshot.

Низкоуровневый import_introspection_schema(sessions, service_id, authorize=...)
требует фабрику **новых** сессий; API передаёт общий authorization callback для
обеих фаз. Доверенный внутренний вызывающий код отвечает за свои права сам.
Метод не получает и не завершает чужую AsyncSession, не коммитит/не откатывает
её dirty/new/flushed изменения. Сервис должен быть committed перед импортом:
невидимая другой транзакции pending-запись намеренно не публикуется автоматически.
Не следует подменять factory возвратом одной caller-owned активной session.

Короткая service lock сериализует SDL/introspection и endpoint edits только
на этапе записи; во время HTTP она не удерживается. Конкурентный импорт после
commit видит тот же hash и возвращает прежний snapshot ID. При storage error
owned transaction/session contexts откатывают весь импорт. См. официальную
[семантику SQLAlchemy SAVEPOINT](https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#using-savepoint).

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
- PostgreSQL pool_size=1/max_overflow=0: cookie/API-key introspection ждёт upstream,
  а независимый GET /api/services выполняется в том же пуле до освобождения upstream;
  checkout равен нулю во время HTTP и после возврата/отмены.
- Отзыв grant/роли/session, expiry/блокировка, удаление сервиса и endpoint change
  во время HTTP не оставляют schemas/types/fields. Чужие dirty/new/flushed изменения
  остаются в caller transaction и не видны наблюдателю до явного caller commit.
- Полный существующий CI с PostgreSQL 16 и реальным Nginx/Docker/Uvicorn smoke.
