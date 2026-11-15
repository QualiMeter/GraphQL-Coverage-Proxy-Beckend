# Frontend: service-aware introspection, этап 3

База: main `09659aed51f91dd6e93d95a3818497901c96349c`, PR #24 принят.
Этот контракт относится к новой ветке; production не меняется при создании PR.

Порядок: **Service → Introspection → SchemaSnapshot → TestRun → GraphQL Proxy → Coverage**.

1. Войти через существующую same-origin cookie-сессию. Нужен admin либо operator
   с grant на выбранный service. Создание сервиса назначает grant создателю.
2. Создать Service с реальным endpointUrl или выбрать существующий.
3. Отправить запрос ниже. Браузер использует относительный путь и cookie,
   Origin и X-CSRF-Token; административный API key в UI не передаётся.
4. Получить SchemaOut, сохранить id как snapshotId и использовать прежний POST /api/runs.
5. Передавать run ID в /graphql, дождаться всех ответов и завершить run. Читать coverage.

```http
POST /api/schemas/import/introspection
Content-Type: application/json
Origin: https://graphql.rebyte.fun
X-CSRF-Token: <csrfToken из /api/auth/session>

{"serviceId":"ca987c43-d56e-478e-9a34-53ca397ed350"}
```

Cookie HttpOnly отправляет браузер автоматически. Ответ `201` (также при dedup):

```json
{"id":"162ef82d-d806-4f08-b901-e08b5d9228e2","serviceId":"ca987c43-d56e-478e-9a34-53ca397ed350","schemaHash":"64 hex SHA-256 символа","source":"introspection","createdAt":"2026-11-15T12:00:00Z"}
```

serviceId — единственное поле входа. URL, headers, Authorization, credentials
в JSON запрещены (422). Никакие входящие Cookie, CSRF, API key или Authorization
не передаются внешнему upstream. Viewer/analyst не могут запускать импорт,
публичность сервиса этого не меняет. Operator без grant получает тот же 404,
что для несуществующего сервиса; URL/конфигурация не раскрываются.
Правила редактирования сервиса оператором с grant сохранены.

Выбирается сохранённый service.endpointUrl, глобальный target не заменяет его.
Legacy NULL endpoint может использовать TARGET_GRAPHQL_URL только при явном
COVERAGE_LEGACY_PROXY=true. Если адрес не настроен — 409; UI предлагает настройку
сервиса или ручной SDL, без подстановки фиктивного endpoint.

**Изменение ошибки:** строковый detail сохранён, к доменным ошибкам добавлен code:

```json
{"detail":"Upstream introspection is disabled; import SDL manually using POST /api/schemas/import","code":"INTROSPECTION_DISABLED"}
```

| HTTP | code | Действие UI |
|---|---|---|
| 404 | SERVICE_NOT_FOUND | Сервис отсутствует или недоступен; не уточнять причину |
| 409 | SERVICE_ENDPOINT_NOT_CONFIGURED | Настроить endpointUrl / импортировать SDL |
| 409 | SERVICE_ENDPOINT_CHANGED | Перечитать сервис, повторить импорт после изменения |
| 409 | UPSTREAM_NOT_ALLOWED | Адрес/DNS IP не соответствует SSRF-политике; исправить конфигурацию |
| 409 | INTROSPECTION_DISABLED | Предложить ручной POST /api/schemas/import с SDL |
| 409 | SCHEMA_IMPORT_CONFLICT | Повторить импорт после конфликта сохранения |
| 502 | DNS_UNAVAILABLE | Проверить доступность DNS upstream |
| 502 | UPSTREAM_UNAVAILABLE | Ошибка соединения/TLS, безопасное сообщение |
| 502 | UPSTREAM_HTTP_ERROR | Upstream вернул 4xx/5xx; проверить доступность, при необходимости SDL |
| 502 | UPSTREAM_REDIRECT | Указать конечный разрешённый HTTPS endpoint; redirects не следуются |
| 502 | INVALID_JSON | Upstream вернул невалидный JSON |
| 502 | UPSTREAM_GRAPHQL_ERRORS | Query отклонён; предложить SDL без показа текста upstream |
| 502 | INVALID_INTROSPECTION | Нет data.__schema, схема невалидна или encoding не поддержан |
| 502 | INTROSPECTION_LIMIT_EXCEEDED | Превышен размер/сложность; предложить SDL |
| 503 | UPSTREAM_CAPACITY_EXHAUSTED | Временно заняты слоты запросов; повторить позже |
| 503 | SCHEMA_STORAGE_UNAVAILABLE | Снимок не удалось сохранить; повторить после восстановления |
| 504 | UPSTREAM_TIMEOUT | DNS/connect/read/общий fetch timeout |

401/403 auth/роль/CSRF, 413 размер тела запроса и 422 DTO сохраняют прежний формат
ошибок без обязательного code. Не рассчитывать на code у произвольной ошибки
другого endpoint. Изменены коды legacy ошибок: отсутствующий target → 409,
запрещённый target → 409, timeout → 504 вместо общего 502.

Отключённая introspection распознаётся по явному extensions.code
INTROSPECTION_DISABLED или известной формулировке GraphQL.js/Apollo об отключении.
Просто HTTP 401/403 или произвольная GraphQL-ошибка этого не доказывает: UI всё
равно может предложить SDL. Не показывать сырой текст ошибок или HTTP body upstream.

Dedup остаётся serviceId + schemaHash по нормализованному SDL; одинаковый SDL
разных сервисов — разные snapshots. Повторный импорт того же hash возвращает
тот же id/createdAt, дубликатов нет. Совпадение с SDL-снимком обогащает его source
и introspection metadata; SDL, hash, IDs типов/полей, runs и observations сохраняются.
Ошибки откатывают весь набор schema/type/field; конкурентные SDL/introspection
импорты сериализованы короткой service lock. HTTP проходит без этой блокировки;
смена endpoint во время fetch даёт SERVICE_ENDPOINT_CHANGED без сохранения.

Миграций и изменений SchemaOut/RunOut нет. SDL download, displayName, расширенные
DTO графа, история, аналитика запросов и безопасный AST здесь не добавляются.
Закрытые API с upstream-аутентификацией пока импортируют SDL: [архитектурное
предложение](service-introspection.md#upstream-аутентификация).
