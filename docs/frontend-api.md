# Контракт для фронтенда — GraphQL Coverage API v0.1

Статус: **готова реализация REST API для БД; перехватчик GraphQL ещё разрабатывается**.

## 1. Настройки подключения

- Локальный API: \`http://localhost:8000\`.
- Vite: \`VITE_API_BASE_URL=http://localhost:8000\`.
- Документация Swagger: \`GET /docs\`.
- Полный OpenAPI: \`GET /openapi.json\` (генерируется FastAPI).
- JSON в запросах и ответах: \`camelCase\`.
- Идентификаторы: строки UUID.
- Время: ISO 8601, UTC.
- CORS для разработки: \`http://localhost:5173\`; другой фронтенд-адрес задаётся через \`CORS_ORIGINS\`.

**Важное разделение:** frontend работает с \`/api/...\`. GraphQL-запросы E2E-тестов
позже будут идти через отдельный \`/graphql\`, который пишет другой бэкендер.
Фронтенд сам не должен имитировать наблюдения и высчитывать покрытие.

## 2. Последовательность вызовов

| Шаг | Метод и путь | Кто вызывает |
|---|---|---|
| 1 | \`GET /api/services\` | UI: список GraphQL-сервисов |
| 2 | \`POST /api/services\` | Настройка: создать сервис |
| 3 | \`POST /api/schemas/import\` | Настройка: загрузить SDL |
| 4 | \`GET /api/schemas?serviceId={uuid}\` | UI: список версий схемы |
| 5 | \`GET /api/schemas/{schemaId}/graph\` | UI: карта схемы без результатов тестов |
| 6 | \`POST /api/runs\` | UI/оператор: начать прогон |
| 7 | \`GET /api/runs?snapshotId={uuid}\` | UI: список прогонов |
| 8 | \`GET /api/runs/{runId}/coverage\` | UI: карта + метрики прогона |
| 9 | \`GET /api/runs/{runId}/requests?limit=100&offset=0\` | UI: журнал запросов |
| 10 | \`POST /api/runs/{runId}/finish\` | UI/оператор: завершить прогон |

Для первого демо достаточно шагов 1–8.

## 3. Создать сервис

\`POST /api/services\` — JSON:

\`\`\`json
{
  "key": "demo-api",
  "name": "Demo GraphQL API"
}
\`\`\`

Ответ \`201\`:

\`\`\`json
{
  "id": "ca987c43-d56e-478e-9a34-53ca397ed350",
  "key": "demo-api",
  "name": "Demo GraphQL API",
  "createdAt": "2026-11-13T13:00:00Z"
}
\`\`\`

\`GET /api/services\` возвращает массив таких объектов.

## 4. Импорт GraphQL-схемы

\`POST /api/schemas/import\`:

\`\`\`json
{
  "serviceId": "ca987c43-d56e-478e-9a34-53ca397ed350",
  "sdl": "type Query { users: [User!]! health: String! } type User { id: ID! name: String! email: String! }"
}
\`\`\`

Ответ \`201\`:

\`\`\`json
{
  "id": "162ef82d-d806-4f08-b901-e08b5d9228e2",
  "serviceId": "ca987c43-d56e-478e-9a34-53ca397ed350",
  "schemaHash": "64-character-sha256-hex-string-generated-by-the-backend",
  "source": "sdl",
  "createdAt": "2026-11-13T13:01:00Z"
}
\`\`\`

Поле \`schemaHash\` в примере пояснительное: настоящий сервер возвращает
64 символа SHA-256.

Если та же схема для того же сервиса уже есть, сервер возвращает существующий снимок
без создания копии. Сейчас поддержан импорт SDL; удалённую introspection
добавит разработчик GraphQL Analyzer.

\`GET /api/schemas?serviceId={uuid}\` возвращает массив снимков.

## 5. Карта GraphQL-схемы

\`GET /api/schemas/{schemaId}/graph\`

Этот запрос удобно использовать для первоначальной отрисовки карты
до запуска тестов. У всех полей \`covered=false\` и \`hits=0\`.

Уточнённый вид ответа — в разделе 7.

## 6. Запуск тестового прогона

\`POST /api/runs\`:

\`\`\`json
{
  "snapshotId": "162ef82d-d806-4f08-b901-e08b5d9228e2",
  "name": "E2E Regression",
  "metadata": {
    "branch": "main"
  }
}
\`\`\`

Ответ \`201\`:

\`\`\`json
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
\`\`\`

\`POST /api/runs/{runId}/finish\` — без тела; ответ содержит
тот же объект со \`status="completed"\` и заполненным \`finishedAt\`.
Повторное завершение возвращает \`409\`.

Один снимок схемы может иметь только один активный прогон одновременно.
Исторических завершённых прогонов может быть сколько угодно.

## 7. Покрытие, поля и связи для карты

\`GET /api/runs/{runId}/coverage\`

Пример для схемы из раздела 4 после успешного обращения
\`query { users { id name } }\`:

\`\`\`json
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
\`\`\`

Для графа используйте:

- \`types[].name\` — название узла.
- \`types[].kind\` — вид GraphQL-типа.
- \`types[].fields\` — поля в узле.
- \`relations[]\` — рёбра между типами (идентификатор ребра: тип + поле).
- \`covered\` — был ли выбран элемент в учтённом запросе.
- \`hits\` — число запросов, затронувших поле (повтор поля в одном запросе не увеличивает hits).
- \`coveragePercent\` — готовое значение сервера, не пересчитывать во фронтенде.

Для типов без учитываемых полей \`coveragePercent=null\` (не показывайте их как 0%).
\`__typename\`, introspection-поля и входные поля типов не входят в знаменатель.
Если запрос не был успешно разобран или GraphQL-ответ не содержал \`data\`,
он не добавляет поля в учтённое покрытие.

Граф схемы для \`GET /api/schemas/{schemaId}/graph\` имеет тот же формат,
но \`runId=null\` и все \`hits=0\`.

## 8. Журнал

\`GET /api/runs/{runId}/requests?limit=100&offset=0\`

Возвращает массив (сортировка от новых к старым):

\`\`\`json
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
\`\`\`

Значения \`analysisStatus\`:
\`success\`, \`parse_error\`, \`validation_error\`, \`unsupported\`, \`internal_error\`.

## 9. Как вызывать из React + TypeScript

\`\`\`ts
const API = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

type CoverageSummary = {
  totalFields: number;
  coveredFields: number;
  coveragePercent: number;
};

async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(API + path);
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
\`\`\`

В финальной версии фронтенда можно заменить \`unknown[]\` типами
\`GraphType[]\` и \`GraphRelation[]\` из описанного JSON.

## 10. Ошибки и ограничения

| HTTP | Значение |
|---|---|
| 201 | Создан сервис, снимок или прогон |
| 200 | Успешный GET или завершение прогона |
| 404 | Не существует запрошенного ресурса |
| 409 | Дублирование сервиса или активного прогона, повторное завершение |
| 422 | Некорректные данные, GraphQL SDL, UUID |
| 500 | Внутренняя ошибка сервера |

Стандартная форма ошибок FastAPI — \`{"detail":"..."}\`, а для ошибок
валидации \`detail\` может содержать список объектов.

**Не ожидайте пока работающий \`POST /graphql\`**: эта часть пишется
отдельным разработчиком. Текущее API не имеет авторизации и должно
разворачиваться только в ограниченном тестовом окружении.
