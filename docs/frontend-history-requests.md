# Этап 4: история сервиса и журнал обращений

Это контракт этапа 4; additive изменения этапа 5 описаны в
[актуальном frontend delta](frontend-final-features.md).

База: main `979c5f9b9078834a92261b53fdc28e9fdff87916` (PR #25).
Same-origin `https://graphql.rebyte.fun`: используйте относительные `/api/...`,
существующую серверную cookie session; административный API key в браузер не передаётся.
GET не требует CSRF; изменяющие маршруты сохраняют прежние Origin/CSRF проверки.
Все ответы `/api` имеют `Cache-Control: no-store` и зависят от текущих прав.

## Изменения для frontend

| Маршрут/DTO | Изменение |
|---|---|
| `GET /api/runs` / `RunOut[]` | Ровно один `serviceId` или `snapshotId`, фильтры, SQL pagination, `X-Total-Count`; DTO сохранён |
| `GET /api/runs/{runId}/requests` / `RequestOut[]` | Фильтры AND, сортировка, `X-Total-Count`; добавлено `operationKey: string \| null` |
| `GET /api/runs/{runId}/requests/{requestId}` / `RequestDetailOut` | Новый маршрут; все поля RequestOut + `countedInCoverage: boolean`, `selectedFields: SelectedFieldOut[]` |
| `SelectedFieldOut` | `typeName: string`, `fieldName: string`, `returnType: string \| null` |

`/coverage`, `/operations`, `/stats` и формулы `query_hash`/`operationKey` сохранены.
Для группы с известными hash и типом операции `operationKey` совпадает с ключом
из `/operations`: SHA-256 UTF-8 JSON `[query_hash,operationName,operationType]`,
`ensure_ascii=true`, без пробелов. Если identity неизвестна — `null`, без выдуманного ключа.

## История

```http
GET /api/runs?serviceId=8d881340-9d33-4a92-b55f-f6d855b49754&status=completed&limit=50&offset=0
HTTP/1.1 200 OK
X-Total-Count: 73
Content-Type: application/json
```

```json
[
  {
    "id": "57b03e76-29a9-4d9c-a961-1d47ba4f850f",
    "snapshotId": "162ef82d-d806-4f08-b901-e08b5d9228e2",
    "name": "E2E Regression",
    "status": "completed",
    "startedAt": "2026-11-15T00:00:00Z",
    "finishedAt": "2026-11-15T00:02:00Z",
    "metadata": {}
  }
]
```

Пример массива сокращён. `X-Total-Count` — количество совпадений до limit/offset.
Ответ остаётся массивом, без envelope. История serviceId охватывает все snapshots,
но каждый элемент сохраняет свой snapshotId: карту исторического run нужно открывать
по этому snapshot, без подстановки текущей версии схемы.

| Query | Правило |
|---|---|
| `snapshotId`, `serviceId` | UUID; ровно один из двух, иначе 422 |
| `status` | Необязательно; `running` или `completed`; omission включает все исторические статусы, в том числе legacy `failed` |
| `from`, `to` | Необязательные ISO-8601 timestamps с секундами и timezone; `[from,to)` по startedAt; при обеих границах from < to |
| `limit` | Необязательно, 1–500. **Отсутствие limit возвращает все совпадения**, включая legacy snapshotId; скрытого ограничения нет |
| `offset` | От 0, default 0; без limit пропускает первые offset и возвращает остаток |

Сортировка `startedAt DESC, id DESC`. Даты нормализуются в UTC; `Z` и явные
смещения принимаются, timezone обязателен. Значения только с датой/без timezone,
epoch numbers, неверные UUID/enums/диапазоны — 422.

Публичная история доступна анонимно; приватная — по grant или admin/CLI.
Недоступный либо отсутствующий scope — 404 **без X-Total-Count**.
`metadata` выдаётся только admin/доверенному CLI; всем остальным `{}`, даже с grant.

Для новой интеграции всегда передавайте limit/offset. Страницы детерминированы
для неизменного набора данных; новые записи и смена статусов между HTTP-запросами
могут сдвигать offset, сохранённый snapshot результата не создаётся.

## Журнал

```http
GET /api/runs/57b03e76-29a9-4d9c-a961-1d47ba4f850f/requests?operationName=GetUsers&typeName=User&fieldName=id&sortBy=durationMs&sortOrder=desc&limit=100&offset=0
HTTP/1.1 200 OK
X-Total-Count: 1
```

```json
[
  {
    "id": "6d950251-5469-42fb-b148-d03f0a840f55",
    "runId": "57b03e76-29a9-4d9c-a961-1d47ba4f850f",
    "operationKey": "0f030140faafe545b46f3bd611c137d045e2588d0e0f7ea4af7daa40ec127849",
    "operationName": "GetUsers",
    "operationType": "query",
    "httpMethod": "POST",
    "httpStatus": 500,
    "analysisStatus": "success",
    "responseHasData": true,
    "graphqlHasErrors": true,
    "durationMs": 42,
    "errorCode": null,
    "createdAt": "2026-11-15T00:00:01Z"
  }
]
```

| Query | Правило |
|---|---|
| `operationKey` | Точное совпадение, 64 lowercase hex символа; берите из `/operations` или RequestOut |
| `operationName` | Точное полное имя, case-sensitive, без substring и лимита 255 символов; omission включает безымянные, пустая строка означает точное пустое имя, не null |
| `operationType` | `query`, `mutation`, `subscription` |
| `typeName` | Любое реально выбранное поле указанного типа в snapshot run |
| `fieldName` | Точное имя поля; обязательно вместе с typeName, иначе 422 |
| `analysisStatus` | `success`, `parse_error`, `validation_error`, `unsupported`, `internal_error` |
| `graphqlHasErrors` | Boolean; false не включает неизвестные NULL |
| `httpStatus` | Целое 100–599, точное совпадение |
| `httpStatusClass` | Целое 1–5; N означает [N00,N99], NULL не совпадает |
| `from`, `to` | `[from,to)` по createdAt, те же UTC/ISO правила, что у истории |
| `sortBy` | `createdAt` (default) или `durationMs` |
| `sortOrder` | `asc` или `desc` (default) |
| `limit`, `offset` | 1–500, default 100; offset ≥ 0, default 0 |

Все фильтры соединяются AND, включая httpStatus + httpStatusClass. Противоречивые
корректные фильтры возвращают `[]` / total 0. Selected-field фильтр работает через
SQL EXISTS: обращение с несколькими совпавшими полями возвращается один раз,
даже если оно не countedInCoverage. Подставленные связи чужого snapshot игнорируются.
Пагинация и count выполняются PostgreSQL, без загрузки журнала в Python.

Сортировка: выбранная колонка, затем `id` **в том же направлении**.
Неизвестные durationMs (`null`) **в конце при asc и desc**, не заменяются нулями.
Пустая страница сохраняет total всех совпадений; пустой run — `[]`, total 0.

## Detail и права

```http
GET /api/runs/57b03e76-29a9-4d9c-a961-1d47ba4f850f/requests/6d950251-5469-42fb-b148-d03f0a840f55
```

Ответ — объект RequestOut из примера, дополненный:

```json
{
  "countedInCoverage": true,
  "selectedFields": [
    {"typeName": "Query", "fieldName": "users", "returnType": "[User!]!"},
    {"typeName": "User", "fieldName": "id", "returnType": "ID!"}
  ]
}
```

Это пример **дополнительных полей**, реальный ответ также содержит все поля RequestOut.
`countedInCoverage = analysisStatus == success AND responseHasData == true`;
HTTP 500/GraphQL errors не отменяют data. Поля берутся из request_field_usage только
snapshot выбранного run, сортируются по typeName/fieldName/returnType, без дублей.
Пустое selectedFields означает отсутствие записанных выбранных полей.

| Principal | История публичная | История приватная | Journal/detail |
|---|---|---|---|
| Anonymous | 200 | 404 | 401 |
| Viewer | 200 | 200 с grant, иначе 404 | 403 даже с grant |
| Analyst/operator | 200 | 200 с grant, иначе 404 | 200 только с grant, даже у публичного сервиса; иначе 404 |
| Admin/доверенный CLI key | 200 | 200 | 200 |

При достаточной роли неизвестные/недоступные run/request, либо request чужого run —
404; валидация неверных UUID — 422. Права проверяются на каждом запросе:
отзыв grant закрывает журнал и приватную историю; снятие публичности закрывает
анонимную историю. Ошибки не содержат total, endpoint или приватных метаданных.
Существующие cookie, HttpOnly/Secure/SameSite и role/CSRF не изменены.

GraphQL-код/variables/токены/cookies/response bodies не добавлены ни в БД, ни в DTO.
`safeGraphqlDocument` и `documentAvailability` в этом этапе **не вводятся**.
Нельзя восстанавливать фиктивный GraphQL query из selectedFields.

Рекомендуемый порядок frontend-интеграции: service history с явной pagination →
открытие run по его snapshotId → таблица requests с total и фильтрами → detail →
переход из `/operations` по operationKey. Для viewer/anonymous не запрашивайте журнал.
P95/analysisStatusCounts/безопасный AST — этап 5; P2 пока исключён.

## Миграция и будущий release

`20261117_history_journal` после `20261116_service_endpoints`:
nullable `observed_requests.operation_key`, backfill точной прежней Python-формулой
keyset-порциями по 1000 строк (включая legacy Unicode), NULL для неизвестной identity.
Существующие запросы/hash/timestamps/snapshots/runs не переписываются.
Индексы:

- `test_runs(snapshot_id, started_at, id)` — snapshot history и service → snapshots;
- `observed_requests(run_id, created_at, id)` — default journal/date ranges;
- `observed_requests(run_id, duration_ms, id)` — duration/range scan внутри run;
- partial `(run_id, operation_key, created_at, id) WHERE operation_key IS NOT NULL` —
  выбор конкретной группы с default chronological pagination.

Сортировки с NULLS LAST и составными фильтрами могут потребовать SQL sort;
точный count и большой offset имеют цену, proportional подходящему журналу.
Количество SQL-запросов не растёт с количеством выбранных полей/обращений.

Ключ материализуется ORM перед insert/update identity; агрегат `/operations`
по-прежнему считает исходную формулу. Прямые сторонние SQL-writers не поддерживаются:
при их добавлении они должны поддерживать этот ключ. Старые процессы необходимо
остановить **до миграции**, чтобы они не добавляли записи без materialized key.
Миграция транзакционная; ADD COLUMN, backfill и обычные CREATE INDEX удерживают
блокировки и могут занять время на больших журналах. Для будущего rollout нужны
backup, оценка размера/времени на копии PostgreSQL и окно обслуживания с остановкой
writers; сначала миграция, затем новая версия backend. Downgrade удаляет только
новый ключ/индексы, без удаления observations, но сначала нужно остановить новую версию.

В рамках PR production не меняется, deploy не включается и merge не выполняется.
Сохраняйте `ENABLE_PRODUCTION_DEPLOY` выключенным при последующем ручном ревью/слиянии.
Локальные проверки: `TEST_DATABASE_URL=<isolated-postgres> pytest`; миграции
проверяются на PostgreSQL, SQLite не используется. Полный workflow дополнительно
проверяет PostgreSQL 16 и Nginx → Docker → Uvicorn cookie/CSRF smoke.
