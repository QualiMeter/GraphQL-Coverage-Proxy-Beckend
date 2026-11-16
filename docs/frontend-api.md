# Контракт для фронтенда — GraphQL Coverage API v0.1

Контракт текущего кода репозитория; состояние развёрнутой версии проверяется
отдельно. [Авторизация и ACL](frontend-auth.md),
[сервисы и маршрутизация](frontend-multi-service.md),
[introspection и коды ошибок](frontend-introspection.md),
[история и журнал](frontend-history-requests.md),
[статистика, безопасный AST и schema metadata](frontend-final-features.md),
[GraphQL error details](graphql-error-details.md). Полные DTO — в `/openapi.json`.

## 1. Настройки подключения

- Production frontend: `https://graphql.rebyte.fun/coverage/`.
- Production REST API: `https://graphql.rebyte.fun/api/...` (**без** `/coverage`).
- Production React-запросы: относительные пути, например `fetch('/api/services')`.
  Фронтенд и API имеют один origin, поэтому CORS между ними не нужен.
- Для локальной cookie-авторизации Vite проксирует `/api` на локальный API;
  COVERAGE_PUBLIC_ORIGIN=http://localhost:5173 и COVERAGE_DEV_MODE=true.
  Не использовать dev Origin для сессий production.
- Отдельно запущенный локальный backend: `http://localhost:8000`
  (при необходимости поменяйте target Vite proxy).
- `VITE_API_BASE_URL` и другие абсолютные URL не требуются для текущей
  production-схемы. При намеренном cross-origin запросе CORS настраивается
  отдельно для конкретного origin; не используйте `*` для авторизованного UI.
- Swagger: `https://graphql.rebyte.fun/docs` (`GET /docs`).
- OpenAPI: `https://graphql.rebyte.fun/openapi.json` (`GET /openapi.json`).
- JSON в запросах и ответах: `camelCase`.
- Идентификаторы: строки UUID.
- Время: ISO 8601, UTC.

Публичные агрегаты доступны без входа. Приватные данные требуют cookie-сессии
и разрешения на сервис; журнал — роли analyst/operator/admin и grant.
Изменяющие операции — operator/admin и grant; создание сервиса автоматически
назначает доступ создателю. Admin управляет пользователями и grants.
Browser session использует HttpOnly cookie и CSRF, см. [контракт](frontend-auth.md).
`X-Coverage-Api-Key` сохраняется только для доверенного CLI/CI и не передаётся
в React/Vite, localStorage или публичный JavaScript. Mock-данные не заменяют ошибки API.

### Публичность и доступ

- Сервисы приватны по умолчанию. Anonymous GET services возвращает только опубликованные;
  сессия — опубликованные и выданные по grant, admin/CLI key — все.
- Приватный UUID без разрешения возвращает 404. Права проверяются через snapshot/run → service.
- `/requests` требует analyst/operator с grant либо admin/CLI, даже для публичного сервиса.
- Произвольное RunOut.metadata доступно только admin/CLI, остальные получают `{}`.
- Unsafe-запросы с сессией требуют Origin и X-CSRF-Token. Без входа — 401,
  недостаточная роль/CSRF — 403. Существующий fail-closed 503 без настроенного
  ключа и без cookie сохранён для legacy CLI-запросов.
- Upstream определяется по run → snapshot → service.endpointUrl. Run ID обязателен.
  Без run ID разрешён только доверенный CLI в явном COVERAGE_LEGACY_PROXY=true.
  Browser proxy всегда требует run ID, даже при наличии API key.
- API-ответы не кешируются; CORS по умолчанию выключен. Для same-origin cookie не нужен CORS.

## 2. Последовательность вызовов

| Шаг | Метод и путь | Кто вызывает |
|---|---|---|
| 1 | `GET /api/services` | Публичный UI: опубликованные GraphQL-сервисы |
| 2 | `POST /api/services` | Operator/admin session или CLI key: создать сервис |
| 3 | `POST /api/schemas/import` | Operator/admin session + grant или CLI key |
| 4 | `GET /api/schemas?serviceId={uuid}` | Публичный UI: версии схемы опубликованного сервиса |
| 5 | `GET /api/schemas/{schemaId}/graph` | Публичный UI: карта схемы до результатов тестов |
| 6 | `POST /api/runs` | Operator/admin session + grant или CLI key |
| 7 | `GET /api/runs?snapshotId={uuid}` или `?serviceId={uuid}` | История с фильтрами, RunOut[] + X-Total-Count, ACL сервиса |
| 8 | `GET /api/runs/{runId}/coverage` | Публичный UI: карта и метрики покрытия |
| 9 | `GET /api/runs/{runId}/requests?limit=100&offset=0` | Analyst/operator session + grant или admin/CLI key |
| 10 | `POST /api/runs/{runId}/finish` | Operator/admin session + grant или CLI key |
| 11 | `PATCH /api/services/{serviceId}` | Operator/admin session + grant или CLI key |
| 12 | `POST /api/schemas/import/introspection` | Operator/admin session + grant или CLI key |
| 13 | `GET /api/runs/{runId}/operations` | Публичный UI: агрегаты операций опубликованного сервиса |
| 14 | `GET /api/runs/{runId}/stats` | Публичный UI: статистика и временная шкала прогона |
| 15 | `GET /api/runs/{runId}/requests/{requestId}` | Analyst/operator с grant либо admin/CLI; безопасный detail |

Для первой интеграции с реальным API достаточно GET-маршрутов 1, 4, 5, 7 и 8.

### Агрегированная аналитика

Реализованы два endpoint для уже существующих страниц фронтенда:

- `GET /api/runs/{runId}/operations` — группировка наблюдений по
  `query_hash`, имени и типу операции, счётчики, ошибки, среднее время и поля
  через `request_field_usage`.
- `GET /api/runs/{runId}/stats` — число обращений/операций, типы, HTTP/GraphQL
  ошибки, временная шкала и показатели существующего Coverage Engine.

Точные DTO — в разделах 8.1–8.2 и OpenAPI. Агрегаты публичного сервиса доступны
анонимно; приватного — с grant либо admin/CLI key. Отзыв публикации/grant
закрывает следующие чтения, включая исторические прогоны.

Фильтры и detail описаны в [контракте журнала](frontend-history-requests.md).
P95, SDL, displayName, schema metadata и безопасный AST — в
[расширенных DTO](frontend-final-features.md). Сравнения прогонов/P50/P99 не реализованы.

## 3. Создать сервис

`POST /api/services` — operator/admin с cookie+CSRF либо доверенный CLI с
`X-Coverage-Api-Key`. JSON:

```json
{
  "key": "demo-api",
  "name": "Demo GraphQL API",
  "endpointUrl": "https://api.example.com/graphql",
  "description": "Основной API",
  "isPublic": false
}
```

Ответ `201`:

```json
{
  "id": "ca987c43-d56e-478e-9a34-53ca397ed350",
  "key": "demo-api",
  "name": "Demo GraphQL API",
  "isPublic": false,
  "createdAt": "2026-11-13T13:00:00Z",
  "updatedAt": "2026-11-13T13:00:00Z",
  "endpointUrl": "https://api.example.com/graphql",
  "description": "Основной API"
}
```

`GET /api/services` анонимно возвращает опубликованные сервисы; с сессией —
также выданные по grant; admin/CLI видит все сервисы.

`GET /api/services/{serviceId}` возвращает тот же DTO с проверкой ACL.
`endpointUrl` доступен только admin/CLI или operator с grant; остальным — null.
`PATCH` поддерживает name, description (null очищает), endpointUrl, isPublic.
Изменение endpoint при активном run любого snapshot сервиса возвращает 409.
Пустой PATCH и null для name/endpointUrl/isPublic дают 422.

Чтобы опубликовать сервис, сервер администратора выполняет
`PATCH /api/services/{serviceId}` с телом `{"isPublic":true}` и
`X-Coverage-Api-Key`. Для скрытия — `{"isPublic":false}`.

## 4. Импорт GraphQL-схемы

`POST /api/schemas/import` — operator/admin с grant и cookie+CSRF либо CLI key:

```json
{
  "serviceId": "ca987c43-d56e-478e-9a34-53ca397ed350",
  "sdl": "type Query { users: [User!]! health: String! } type User { id: ID! name: String! email: String! }"
}
```

Ответ `201`:

```json
{
  "id": "162ef82d-d806-4f08-b901-e08b5d9228e2",
  "serviceId": "ca987c43-d56e-478e-9a34-53ca397ed350",
  "schemaHash": "64-character-sha256-hex-string-generated-by-the-backend",
  "source": "sdl",
  "createdAt": "2026-11-13T13:01:00Z"
}
```

Поле `schemaHash` в примере пояснительное: настоящий сервер возвращает
64 символа SHA-256.

Если та же схема для того же сервиса уже есть, сервер возвращает существующий снимок
без создания копии. Кроме SDL, реализован
`POST /api/schemas/import/introspection` с телом
`{"serviceId":"<UUID>"}`. Адрес берётся из endpointUrl выбранного сервиса;
настроенные сервисы всегда используют свой адрес. URL, headers и credentials в
теле запрещены. Для старых NULL endpoint остаётся только явный legacy fallback.
Ошибки имеют строковый detail и стабильный code; отключённая introspection —
409 INTROSPECTION_DISABLED с предложением SDL. Полная [таблица и DTO](frontend-introspection.md).
Повторный hash возвращает тот же snapshot; совпавший SDL-снимок обогащается
source/introspection metadata без изменения IDs полей, runs и observations.

`GET /api/schemas?serviceId={uuid}` возвращает снимки опубликованного сервиса
либо сервиса с grant; admin/CLI видит любой. Неизвестный/недоступный service — 404.

## 5. Карта GraphQL-схемы

`GET /api/schemas/{schemaId}/graph`

Этот запрос удобно использовать для первоначальной отрисовки карты
до запуска тестов. У всех полей `covered=false` и `hits=0`.

Уточнённый вид ответа — в разделе 7.

## 6. Запуск тестового прогона

`POST /api/runs` — operator с grant / admin с cookie+CSRF либо доверенный CLI key:

```json
{
  "snapshotId": "162ef82d-d806-4f08-b901-e08b5d9228e2",
  "name": "E2E Regression",
  "metadata": {
    "branch": "main"
  }
}
```

Ответ `201`:

```json
{
  "id": "57b03e76-29a9-4d9c-a961-1d47ba4f850f",
  "snapshotId": "162ef82d-d806-4f08-b901-e08b5d9228e2",
  "name": "E2E Regression",
  "status": "running",
  "startedAt": "2026-11-13T13:05:00Z",
  "finishedAt": null,
  "metadata": {
    "branch": "main"
  }
}
```

`POST /api/runs/{runId}/finish` — operator с grant/admin и cookie+CSRF либо CLI key,
без тела; ответ содержит
тот же объект со `status="completed"` и заполненным `finishedAt`.
Повторное завершение возвращает `409`.

Один снимок схемы может иметь только один активный прогон одновременно.
Исторических завершённых прогонов может быть сколько угодно.

`GET /api/runs` принимает ровно один `snapshotId` либо `serviceId` (все snapshots
сервиса), фильтры `status`, UTC `[from,to)`, `limit` 1–500 и `offset` ≥ 0.
Без limit возвращаются **все** совпадения, сохраняя старый snapshotId-клиент.
Ответ RunOut[] и `X-Total-Count` до pagination; порядок startedAt DESC/id DESC.
metadata — только admin/CLI, остальным `{}`. Недоступная история — 404 без total.
[Полный контракт истории, DTO и примеры](frontend-history-requests.md).

## 7. Покрытие, поля и связи для карты

`GET /api/runs/{runId}/coverage`

Пример основных полей графа (дополнительные schema metadata опущены)
для схемы из раздела 4 после успешного обращения
`query { users { id name } }`:

```json
{
  "schemaId": "162ef82d-d806-4f08-b901-e08b5d9228e2",
  "runId": "57b03e76-29a9-4d9c-a961-1d47ba4f850f",
  "summary": {
    "totalFields": 5,
    "coveredFields": 3,
    "coveragePercent": 60.0
  },
  "types": [
    {
      "name": "Query",
      "kind": "OBJECT",
      "totalFields": 2,
      "coveredFields": 1,
      "coveragePercent": 50.0,
      "fields": [
        {
          "name": "health",
          "returnType": "String!",
          "targetType": null,
          "deprecated": false,
          "covered": false,
          "hits": 0
        },
        {
          "name": "users",
          "returnType": "[User!]!",
          "targetType": "User",
          "deprecated": false,
          "covered": true,
          "hits": 1
        }
      ]
    },
    {
      "name": "User",
      "kind": "OBJECT",
      "totalFields": 3,
      "coveredFields": 2,
      "coveragePercent": 66.67,
      "fields": [
        {
          "name": "email",
          "returnType": "String!",
          "targetType": null,
          "deprecated": false,
          "covered": false,
          "hits": 0
        },
        {
          "name": "id",
          "returnType": "ID!",
          "targetType": null,
          "deprecated": false,
          "covered": true,
          "hits": 1
        },
        {
          "name": "name",
          "returnType": "String!",
          "targetType": null,
          "deprecated": false,
          "covered": true,
          "hits": 1
        }
      ]
    }
  ],
  "relations": [
    {
      "fromType": "Query",
      "field": "users",
      "toType": "User",
      "covered": true
    }
  ]
}
```

Для графа используйте:

- `types[].name` — название узла.
- `types[].kind` — вид GraphQL-типа.
- `types[].fields` — поля в узле.
- `relations[]` — рёбра между типами; `id` — UUIDv5 от snapshot и координат ребра
  ([точная формула](frontend-final-features.md)).
- `covered` — был ли выбран элемент в учтённом запросе.
- `hits` — число запросов, затронувших поле (повтор поля в одном запросе не увеличивает hits).
- `coveragePercent` — готовое значение сервера, не пересчитывать во фронтенде.

Для типов без учитываемых полей `coveragePercent=null` (не показывайте их как 0%).
`__typename`, introspection-поля и входные поля типов не входят в знаменатель.
Если запрос не был успешно разобран или GraphQL-ответ не содержал `data`,
он не добавляет поля в учтённое покрытие.

Граф схемы для `GET /api/schemas/{schemaId}/graph` имеет тот же формат,
но `runId=null` и все `hits=0`.

## 8. Журнал

`GET /api/runs/{runId}/requests?limit=100&offset=0`

Analyst/operator с grant к сервису либо admin/доверенный CLI. Публичность сервиса
не открывает журнал: anonymous — 401, viewer — 403, отсутствующий grant — 404.
API key в браузер не передаётся. Возвращается RequestOut[] и `X-Total-Count`.
Добавлено `operationKey: string | null`, совпадающее с `/operations`.

Фильтры: operationKey, case-sensitive operationName, operationType, typeName,
fieldName (только вместе с typeName), analysisStatus, graphqlHasErrors, httpStatus,
httpStatusClass (1–5), UTC from/to `[from,to)`. Все соединяются AND.
sortBy=createdAt|durationMs, sortOrder=asc|desc; defaults createdAt/desc.
id — tie-breaker в том же направлении, duration null всегда в конце.
limit 1–500 (default 100), offset ≥ 0. SQL EXISTS исключает дубли полей.

Новый `GET /api/runs/{runId}/requests/{requestId}` возвращает все поля RequestOut,
countedInCoverage (`success AND responseHasData=true`) и уникальные selectedFields
с typeName/fieldName/returnType из точного snapshot. Чужой request/run — 404.
Raw query, variables, cookies и response bodies не сохраняются.
В detail добавлены safeGraphqlDocument/documentAvailability: нормализованная
AST-копия реально пересланного документа, литералы/defaults маскируются;
исторические записи — unavailable/null. Доступ только по journal ACL.
[Состояния и лимиты](frontend-final-features.md). Фиктивный query не восстанавливается.
[Подробности журнала](frontend-history-requests.md). Дополнительные
`graphqlErrorCount`, `graphqlErrorDetailsAvailability`, `graphqlErrorDetails`
описаны в [контракте ошибок upstream](graphql-error-details.md); они есть только в detail.

Пример списка:

```json
[
  {
    "id": "6d950251-5469-42fb-b148-d03f0a840f55",
    "runId": "57b03e76-29a9-4d9c-a961-1d47ba4f850f",
    "operationKey": "0f030140faafe545b46f3bd611c137d045e2588d0e0f7ea4af7daa40ec127849",
    "operationName": "GetUsers",
    "operationType": "query",
    "httpMethod": "POST",
    "httpStatus": 200,
    "analysisStatus": "success",
    "responseHasData": true,
    "graphqlHasErrors": false,
    "durationMs": 42,
    "errorCode": null,
    "createdAt": "2026-11-13T13:06:00Z"
  }
]
```

Значения `analysisStatus`:
`success`, `parse_error`, `validation_error`, `unsupported`, `internal_error`.

### 8.1. Операции прогона

`GET /api/runs/{runId}/operations` возвращает `OperationOut[]`.
Для существующего пустого прогона — `[]`. Доступ описан в разделе 2.

```ts
type OperationType = "query" | "mutation" | "subscription";

type OperationFieldOut = {
  typeName: string;
  fieldName: string;
  hits: number; // целое, число учтённых обращений, выбравших поле
};

type OperationOut = {
  operationKey: string;
  operationName: string | null;
  operationType: OperationType;
  requestCount: number;
  countedRequestCount: number;
  graphqlErrorCount: number;
  httpErrorCount: number;
  avgDurationMs: number | null;
  lastSeenAt: string; // ISO 8601 UTC
  fields: OperationFieldOut[];
};
```

Пример ответа на `GET /api/runs/{runId}/operations?operationName=GetUsers&operationType=query`:

```json
[
  {
    "operationKey": "0f030140faafe545b46f3bd611c137d045e2588d0e0f7ea4af7daa40ec127849",
    "operationName": "GetUsers",
    "operationType": "query",
    "requestCount": 3,
    "countedRequestCount": 2,
    "graphqlErrorCount": 1,
    "httpErrorCount": 1,
    "avgDurationMs": 21.0,
    "lastSeenAt": "2026-11-13T13:07:10Z",
    "fields": [
      {"typeName": "Query", "fieldName": "users", "hits": 2},
      {"typeName": "User", "fieldName": "id", "hits": 2},
      {"typeName": "User", "fieldName": "name", "hits": 2}
    ]
  }
]
```

Правила:

- Группа определяется кортежем `(query_hash, operation_name, operation_type)`.
  Одинаковые имена при разных хешах, а также разные имена/типы при одном хеше
  образуют разные группы. Безымянные операции имеют `operationName: null`.
- `operationKey` — SHA-256 от UTF-8 JSON-массива
  `[query_hash, operation_name, operation_type]` без пробелов, с ASCII-экранированием
  (`json.dumps(..., ensure_ascii=True, separators=(",", ":"))`). Он одинаков
  для одной группы в разных прогонах; идентификатор не зависит от UUID и времени.
  Существующий хеш вычислен от текста документа: форматирование тоже может
  менять группу. API не сохраняет исходный текст, переменные, тела ответов или токены;
  разрешена только masked AST copy в защищённом detail.
- Нужны известные `query_hash` и `operation_type`. Обращения без этих данных
  (например, ошибка разбора или старое наблюдение без хеша) не получают
  фиктивной группы и остаются в `stats.totalRequests` и временной шкале.
  Ошибки валидации/неподдержанные операции с известной идентичностью входят в
  соответствующую группу, но не создают поля покрытия.
- `requestCount` считает все наблюдения группы один раз. `countedRequestCount`
  использует условие Coverage Engine: `analysis_status=success` и
  `response_has_data=true`. Прокси устанавливает последний флаг только для
  объекта `data`; `data: null` не учитывается. GraphQL-ошибки могут
  сосуществовать с данными, а HTTP-ошибка не отменяет это условие.
- `fields` имеет контракт `[{typeName, fieldName, hits}]` и содержит координаты
  полей из `request_field_usage`, выбранных хотя бы одним учтённым обращением.
  `hits` использует то же условие Coverage Engine, что `countedRequestCount`:
  `analysis_status=success AND response_has_data=true`. Повтор одного поля или
  его alias в одном обращении считается один раз. В примере три успешных
  анализа, но только два ответа с объектом `data`, поэтому `hits=2`.
  Обращения с `response_has_data=false` или неизвестным флагом не увеличивают
  `hits`; выбранные только в них поля отсутствуют в списке. Без учтённых полей
  возвращается `fields: []`. HTTP/GraphQL-ошибки сами по себе не исключают
  обращение с успешным анализом и данными. Это число учтённых обращений,
  а не число успешных resolver-вызовов. Поля отсортированы по `typeName`, затем
  `fieldName`. Метрики `requestCount` внутри `fields` нет; на уровне операции
  она по-прежнему считает все обращения группы.
- `httpErrorCount` — HTTP 400–599; `graphqlErrorCount` — сохранённый флаг
  `graphql_has_errors=true`. Счётчики независимы, обращение может увеличить оба.
  Неизвестный статус/флаг не считается ошибкой.
- `avgDurationMs` — среднее только известных `duration_ms`, включая нулевые;
  без измерений возвращается `null`. `lastSeenAt` — максимум `created_at` группы.
- Необязательные query-параметры `operationName` и `operationType` применяются
  совместно. Имя сравнивается точно, с учётом регистра; тип — одно из трёх
  значений DTO, иначе `422`. Без фильтров возвращаются и безымянные операции;
  отдельного фильтра «только безымянные» нет. Пагинации нет. Сортировка групп:
  хеш, имя (`null` в конце), тип.
- `subscription` отражает только уже сохранённый тип. Анализатор по-прежнему
  возвращает `unsupported`, без полей и без искусственного покрытия.

### 8.2. Статистика прогона

`GET /api/runs/{runId}/stats` возвращает `RunStats`. Фильтров нет;
статистика всегда относится ко всему указанному прогону и его снимку схемы.

```ts
type RequestTimelineBucket = {
  bucketStart: string; // ISO 8601 UTC, включительная граница
  requestCount: number;
  countedRequestCount: number;
  graphqlErrorCount: number;
  httpErrorCount: number;
  avgDurationMs: number | null;
};

type RunStats = {
  analysisStatusCounts: {
    success: number; parseError: number; validationError: number;
    unsupported: number; internalError: number;
  };
  p95DurationMs: number | null;
  totalRequests: number;
  countedRequests: number;
  uniqueOperations: number;
  queryRequests: number;
  mutationRequests: number;
  subscriptionRequests: number;
  httpErrorCount: number;
  graphqlErrorCount: number;
  avgDurationMs: number | null;
  totalFields: number;
  coveredFields: number;
  coveragePercent: number;
  requestTimeline: RequestTimelineBucket[];
};
```

Пример для схемы из раздела 4: три `GetUsers`, одно безымянное обращение к
`health` и одна ошибка разбора. Только два `GetUsers` и обращение к `health`
учтены в покрытии. Длительности известны у трёх обращений: 14, 28 и 18 мс.

```json
{
  "totalRequests": 5,
  "countedRequests": 3,
  "uniqueOperations": 2,
  "queryRequests": 4,
  "mutationRequests": 0,
  "subscriptionRequests": 0,
  "httpErrorCount": 2,
  "graphqlErrorCount": 2,
  "avgDurationMs": 20.0,
  "p95DurationMs": 27.0,
  "analysisStatusCounts": {"success": 4, "parseError": 1, "validationError": 0, "unsupported": 0, "internalError": 0},
  "totalFields": 5,
  "coveredFields": 4,
  "coveragePercent": 80.0,
  "requestTimeline": [
    {
      "bucketStart": "2026-11-13T13:06:00Z",
      "requestCount": 2,
      "countedRequestCount": 2,
      "graphqlErrorCount": 1,
      "httpErrorCount": 1,
      "avgDurationMs": 21.0
    },
    {
      "bucketStart": "2026-11-13T13:07:00Z",
      "requestCount": 3,
      "countedRequestCount": 1,
      "graphqlErrorCount": 1,
      "httpErrorCount": 1,
      "avgDurationMs": 18.0
    }
  ]
}
```

- `totalRequests` включает все `observed_requests`, в том числе ошибки разбора,
  валидации, неподдержанные запросы, внутренние ошибки анализа и записи без
  HTTP-статуса/длительности/идентичности. `countedRequests` использует то же
  условие, что `countedRequestCount` операций.
- `uniqueOperations` — число групп по правилам `/operations`, без фильтров.
  `queryRequests`, `mutationRequests`, `subscriptionRequests` считают сохранённый
  тип всех наблюдений, независимо от успешности анализа. При неизвестных типах
  их сумма может быть меньше `totalRequests`.
- Ошибки и среднее время считаются так же, как в `/operations`. Среднее для
  прогона считается по измерениям, а не как среднее из средних по группам.
- `totalFields`, `coveredFields`, `coveragePercent` берутся из того же Coverage
  Engine, что `GET /api/runs/{runId}/coverage`, с тем же знаменателем и округлением.
  Повторные обращения не увеличивают число уникальных покрытых полей.
  Фронтенд использует готовое значение, не рассчитывает свой процент.
- `requestTimeline` отсортирован по возрастанию `bucketStart`. Размер интервала
  фиксирован: **60 секунд**, `[bucketStart, bucketStart + 60 секунд)` в **UTC**.
  Источник времени — `observed_requests.created_at`; часовой пояс сессии БД
  и границы суток не меняют интервалы. Пустые интервалы пропущены (UI при
  необходимости заполняет их нулями), искусственных наблюдений нет.
  Все счётчики интервала имеют ту же семантику, что счётчики всего прогона;
  среднее исключает неизвестные длительности, без измерений равно `null`.
- Пустой прогон: все счётчики обращений и операций равны `0`,
  `avgDurationMs: null`, `requestTimeline: []`, `coveredFields: 0` и
  `coveragePercent: 0.0`. `totalFields` остаётся размером схемы по Coverage Engine;
  если учитываемых полей нет, он равен `0`.
- Завершённые/неуспешные исторические прогоны доступны по тем же правилам.
  Новый активный прогон или новая версия схемы не меняет их агрегаты.

## 9. Как вызывать из React + TypeScript

```ts
// Относительный путь работает в production и через локальный Vite proxy.
// Административный API-ключ в браузер не передаём.
type CoverageSummary = {
  totalFields: number;
  coveredFields: number;
  coveragePercent: number;
};

async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error("API returned HTTP " + response.status);
  }
  return (await response.json()) as T;
}

export async function getRunCoverage(runId: string) {
  return apiGet<{ summary: CoverageSummary; types: unknown[]; relations: unknown[] }>(
    "/api/runs/" + encodeURIComponent(runId) + "/coverage"
  );
}

export async function getPublicServices() {
  return apiGet<Array<{
    id: string;
    key: string;
    name: string;
    isPublic: boolean;
    createdAt: string;
  }>>("/api/services");
}
```

В финальной версии фронтенда можно заменить `unknown[]` типами
`GraphType[]` и `GraphRelation[]` из описанного JSON.

## 10. Ошибки и ограничения

| HTTP | Значение |
|---|---|
| 201 | Создан сервис, снимок или прогон |
| 200 | Успешный GET или завершение прогона |
| 401 | Нет действующей сессии/API key для защищённой операции |
| 403 | Недостаточная роль либо неверный Origin/CSRF |
| 404 | Ресурс не существует **или скрыт** от публичного клиента |
| 409 | Дублирование сервиса или активного прогона, повторное завершение |
| 422 | Некорректные данные, GraphQL SDL, UUID |
| 502/504 | Проблема с upstream GraphQL, если запрашивается прокси |
| 503 | Административный ключ или GraphQL upstream не настроен (в соответствующем маршруте) |
| 500 | Внутренняя ошибка сервера |

Стандартная форма ошибок FastAPI — `{"detail":"..."}`, а для ошибок
валидации `detail` может содержать список объектов.

`POST /graphql` доступен operator/admin с cookie+CSRF и grant либо доверенному CLI
с административным ключом. Browser-запрос требует X-Coverage-Run-Id.
У него могут быть дополнительные заголовки `X-Coverage-Run-Id` и upstream
`Authorization`; первый связывает операцию с активным E2E-прогоном.
Не отправляйте `X-Coverage-Api-Key` в браузере, даже если Swagger позволяет
вручную авторизоваться: секрет доступен владельцам серверного окружения.
