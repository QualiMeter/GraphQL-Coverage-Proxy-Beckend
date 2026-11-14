# Контракт для фронтенда — GraphQL Coverage API v0.1

Статус: **REST API и GraphQL HTTP-прокси работают в production**.
React-фронтенд развёрнут по `https://graphql.rebyte.fun/coverage/` и пока
использует локальные demo/mock-данные. Некоторые операции требуют серверного
API-ключа. Агрегированная аналитика операций и статистики реализована в этом
контракте; доступность на production зависит от выкладки соответствующей версии.

## 1. Настройки подключения

- Production frontend: `https://graphql.rebyte.fun/coverage/`.
- Production REST API: `https://graphql.rebyte.fun/api/...` (**без** `/coverage`).
- Production React-запросы: относительные пути, например `fetch('/api/services')`.
  Фронтенд и API имеют один origin, поэтому CORS между ними не нужен.
- Локальный Vite (`http://localhost:5173`) уже проксирует `/api` на
  `https://graphql.rebyte.fun`; тот же относительный `fetch` работает локально.
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

**Важное разделение:** публичный React-фронтенд использует только открытые
`GET /api/...` для опубликованных сервисов. Административные операции и
`POST /graphql` доступны только доверенному серверу/CI через заголовок
`X-Coverage-Api-Key`. **Нельзя** помещать этот ключ в `VITE_*`, JavaScript,
`localStorage` или код приложения: он будет доступен всем посетителям.
Для административного UI понадобится серверная аутентификация пользователей
и проверка прав на конкретный сервис; одной открытой регистрации недостаточно.
Регистрации у публичного каталога нет. После API-интеграции фронтенд должен
показывать реальные наблюдения и готовые метрики сервера, а не mock-данные
или собственную альтернативную формулу покрытия.

### Публичность и доступ

- Сервисы по умолчанию приватные (`isPublic: false`). Анонимный
  `GET /api/services` возвращает только опубликованные, поэтому `[]` —
  нормальный ответ даже при наличии записей в БД.
- Анонимные запросы схем, графов, прогонов и покрытия разрешены только для
  опубликованных сервисов. Для приватного ресурса API возвращает `404`.
- `GET /api/runs/{runId}/requests` (журнал запросов) всегда закрыт ключом;
  поле `metadata` публичного прогона скрыто и возвращается как `{}`.
- Создание/изменение сервисов, импорт схем, запуск/завершение прогонов и
  `POST /graphql` требуют ключ. Без ключа — `401`; если ключ вообще не
  настроен на сервере — `503` (закрытый по умолчанию доступ).
- На данный момент `POST /graphql` проксирует запросы к серверному
  `TARGET_GRAPHQL_URL`. Если upstream не настроен, после успешной проверки
  ключа будет `503`; REST API при этом работает.

## 2. Последовательность вызовов

| Шаг | Метод и путь | Кто вызывает |
|---|---|---|
| 1 | `GET /api/services` | Публичный UI: опубликованные GraphQL-сервисы |
| 2 | `POST /api/services` | Сервер администратора/CI: создать сервис (ключ) |
| 3 | `POST /api/schemas/import` | Сервер администратора/CI: загрузить SDL (ключ) |
| 4 | `GET /api/schemas?serviceId={uuid}` | Публичный UI: версии схемы опубликованного сервиса |
| 5 | `GET /api/schemas/{schemaId}/graph` | Публичный UI: карта схемы до результатов тестов |
| 6 | `POST /api/runs` | Сервер администратора/CI: начать прогон (ключ) |
| 7 | `GET /api/runs?snapshotId={uuid}` | Публичный UI: прогоны опубликованного сервиса |
| 8 | `GET /api/runs/{runId}/coverage` | Публичный UI: карта и метрики покрытия |
| 9 | `GET /api/runs/{runId}/requests?limit=100&offset=0` | Сервер администратора: журнал (ключ) |
| 10 | `POST /api/runs/{runId}/finish` | Сервер администратора/CI: завершить прогон (ключ) |
| 11 | `PATCH /api/services/{serviceId}` | Сервер администратора: переключить видимость (ключ) |
| 12 | `POST /api/schemas/import/introspection` | Сервер администратора: импортировать через upstream (ключ) |
| 13 | `GET /api/runs/{runId}/operations` | Публичный UI: агрегаты операций опубликованного сервиса |
| 14 | `GET /api/runs/{runId}/stats` | Публичный UI: статистика и временная шкала прогона |

Для первой интеграции с реальным API достаточно GET-маршрутов 1, 4, 5, 7 и 8.

### Агрегированная аналитика

Реализованы два endpoint для уже существующих страниц фронтенда:

- `GET /api/runs/{runId}/operations` — группировка наблюдений по
  `query_hash`, имени и типу операции, счётчики, ошибки, среднее время и поля
  через `request_field_usage`.
- `GET /api/runs/{runId}/stats` — число обращений/операций, типы, HTTP/GraphQL
  ошибки, временная шкала и показатели существующего Coverage Engine.

Точные DTO и примеры — в разделах 8.1–8.2 и OpenAPI. Агрегаты опубликованного
сервиса доступны без ключа. Для приватного сервиса нужен действующий
`X-Coverage-Api-Key` из доверенного серверного окружения; анонимный запрос или
неверный ключ получает `404`, как для отсутствующего ресурса. Проверка идёт по
связи прогон → снимок схемы → сервис и применяется также к историческим прогонам.
Отзыв `isPublic` немедленно закрывает следующие анонимные чтения. Публичное
чтение работает и без настроенного административного ключа на сервере.
Правила административных маршрутов, журнала и `/graphql` не меняются;
пользовательской регистрации и новой системы авторизации нет.

**P1:** фильтры защищённого `GET /api/runs/{runId}/requests`, подробности
`GET /api/runs/{runId}/requests/{requestId}` и сравнение прогонов.
Пока они также не являются частью реализованного публичного контракта.

## 3. Создать сервис

`POST /api/services` — **административный** запрос с заголовком
`X-Coverage-Api-Key` (выполняется только из доверенного окружения), JSON:

```json
{
  "key": "demo-api",
  "name": "Demo GraphQL API",
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
  "createdAt": "2026-11-13T13:00:00Z"
}
```

`GET /api/services` без ключа возвращает массив **только опубликованных**
сервисов. С корректным административным ключом — все сервисы.

Чтобы опубликовать сервис, сервер администратора выполняет
`PATCH /api/services/{serviceId}` с телом `{"isPublic":true}` и
`X-Coverage-Api-Key`. Для скрытия — `{"isPublic":false}`.

## 4. Импорт GraphQL-схемы

`POST /api/schemas/import` — только с `X-Coverage-Api-Key`:

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
`{"serviceId":"<UUID>"}`. Адрес upstream задаётся **только** на сервере через
`TARGET_GRAPHQL_URL`, передать произвольный URL в запросе нельзя.

`GET /api/schemas?serviceId={uuid}` возвращает массив снимков, если сервис
опубликован (либо запрос выполнен администратором).

## 5. Карта GraphQL-схемы

`GET /api/schemas/{schemaId}/graph`

Этот запрос удобно использовать для первоначальной отрисовки карты
до запуска тестов. У всех полей `covered=false` и `hits=0`.

Уточнённый вид ответа — в разделе 7.

## 6. Запуск тестового прогона

`POST /api/runs` — только с `X-Coverage-Api-Key`:

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

`POST /api/runs/{runId}/finish` — с административным ключом, без тела; ответ содержит
тот же объект со `status="completed"` и заполненным `finishedAt`.
Повторное завершение возвращает `409`.

Один снимок схемы может иметь только один активный прогон одновременно.
Исторических завершённых прогонов может быть сколько угодно.

## 7. Покрытие, поля и связи для карты

`GET /api/runs/{runId}/coverage`

Пример для схемы из раздела 4 после успешного обращения
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
- `relations[]` — рёбра между типами (идентификатор ребра: тип + поле).
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

**Только с административным ключом.** Публичный UI не должен вызывать этот
маршрут: без ключа он возвращает `401`.
Для доверенного клиента возвращается массив (сортировка от новых к старым):

```json
[
  {
    "id": "6d950251-5469-42fb-b148-d03f0a840f55",
    "runId": "57b03e76-29a9-4d9c-a961-1d47ba4f850f",
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
  менять группу. API не сохраняет текст, переменные, тела ответов или токены.
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
| 401 | Нет административного ключа или он неверен для защищённой операции |
| 404 | Ресурс не существует **или скрыт** от публичного клиента |
| 409 | Дублирование сервиса или активного прогона, повторное завершение |
| 422 | Некорректные данные, GraphQL SDL, UUID |
| 502/504 | Проблема с upstream GraphQL, если запрашивается прокси |
| 503 | Административный ключ или GraphQL upstream не настроен (в соответствующем маршруте) |
| 500 | Внутренняя ошибка сервера |

Стандартная форма ошибок FastAPI — `{"detail":"..."}`, а для ошибок
валидации `detail` может содержать список объектов.

`POST /graphql` реализован, но предназначен для доверенного клиента
с административным ключом, не для публичного React-приложения.
У него могут быть дополнительные заголовки `X-Coverage-Run-Id` и upstream
`Authorization`; первый связывает операцию с активным E2E-прогоном.
Не отправляйте `X-Coverage-Api-Key` в браузере, даже если Swagger позволяет
вручную авторизоваться: секрет доступен владельцам серверного окружения.
