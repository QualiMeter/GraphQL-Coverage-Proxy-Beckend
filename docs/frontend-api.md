# Контракт для фронтенда — GraphQL Coverage API v0.1

Статус: **REST API и GraphQL HTTP-прокси работают в production**.
React-фронтенд развёрнут по `https://graphql.rebyte.fun/coverage/` и пока
использует локальные demo/mock-данные. Некоторые операции требуют серверного
API-ключа; новые маршруты аналитики операций и статистики пока в плане.

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

Для первой интеграции с реальным API достаточно GET-маршрутов 1, 4, 5, 7 и 8.

### Следующий этап — агрегированная аналитика (пока не реализовано)

Предложены два новых **P0** endpoint для уже существующих страниц фронтенда:

- `GET /api/runs/{runId}/operations` — группировка наблюдений по
  `query_hash`, имени и типу операции, счётчики, ошибки, среднее время и поля
  через `request_field_usage`.
- `GET /api/runs/{runId}/stats` — число обращений/операций, типы, HTTP/GraphQL
  ошибки, временная шкала и показатели существующего Coverage Engine.

**Не вызывайте эти маршруты как готовые:** сначала требуется реализация и
закрепление точных DTO в OpenAPI. План доступа: агрегаты опубликованного сервиса
публичны, приватного — доступны только авторизованному клиенту.

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
