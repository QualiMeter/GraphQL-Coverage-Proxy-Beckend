# Статистика, безопасный AST и метаданные snapshot
Same-origin: относительные `/api/...`, серверная Secure/HttpOnly/SameSite cookie,
существующий Origin/CSRF для изменений. API key остаётся только доверенному CLI/E2E.
Все API ответы сохраняют `Cache-Control: no-store`; повторные чтения проверяют текущие роли/grants.

## Изменения контрактов

| Endpoint / DTO | Изменение |
|---|---|
| `GET /api/runs/{runId}/stats`, RunStats | `analysisStatusCounts`, `p95DurationMs` |
| `GET /api/runs/{runId}/requests/{requestId}`, RequestDetailOut | `safeGraphqlDocument`, `documentAvailability` |
| `GET /api/schemas/{schemaId}/sdl`, SchemaSdlOut | Новый read endpoint: `{schemaId, schemaHash, sdl}` |
| `PATCH /api/schemas/{schemaId}`, SchemaUpdate → SchemaOut | Новый защищённый endpoint изменения только displayName |
| SchemaOut | Nullable `displayName` во всех существующих ответах import/list |
| CoverageGraph (`/schemas/{id}/graph`, `/runs/{id}/coverage`) | `rootTypes`; расширены GraphType/GraphField/GraphRelation |

Все изменения read DTO additive; старые поля/маршруты сохранены. Отсутствие
знакомого frontend новых полей не должно мешать чтению старых. Formula Coverage
Engine, schemaHash, query_hash и operationKey не меняются. История, операции,
фильтры и X-Total-Count описаны в [контракте журнала](frontend-history-requests.md).

## RunStats

Дополнительные поля объекта (остальные существующие поля остаются):

```json
{
  "totalRequests": 5,
  "analysisStatusCounts": {
    "success": 1,
    "parseError": 1,
    "validationError": 1,
    "unsupported": 1,
    "internalError": 1
  },
  "avgDurationMs": 32.5,
  "p95DurationMs": 88.0
}
```

`analysis_status` берётся из PostgreSQL: success/parse_error/validation_error/
unsupported/internal_error. Пять COUNT FILTER и total считаются одним SQL aggregate
внутри run, поэтому сумма категорий равна totalRequests. Пустой run возвращает
пять нулей. Ошибки анализа для KPI: **parseError + validationError + internalError**;
unsupported отображать отдельно. HTTP и GraphQL ошибки могут пересекаться с ними.

P95 — `percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)` по известным
измерениям только выбранного run, NULL исключаются. Нулевые duration учитываются.
Используется непрерывная линейная интерполяция: отсортировать N измерений, взять
позицию `(N-1)*0.95` с отсчётом от нуля и интерполировать между соседями.
Для `[0, 10, 20, 100]` P95 = 88; avg = 32.5. Для одного значения P95 равно ему,
для нуля известных значений avg/P95 = null. RequestTimeline и правила counted
сохранены: `success AND responseHasData=true`, независимо от HTTP/GraphQL ошибок.
Доступ к stats остаётся по публичности/grant/admin, безопасный документ туда не входит.

## Реальный безопасный документ

Пример дополнительных полей **RequestDetailOut**:

```json
{
  "safeGraphqlDocument": "query Fetch($id: ID = \"[REDACTED]\") {\n  alias: user(id: $id) {\n    ...Fields\n  }\n}\n\nfragment Fields on User {\n  name\n}",
  "documentAvailability": "redacted"
}
```

Источник — весь GraphQL document из реально пересланного proxy тела: все операции,
fragments, directives, aliases, условия типов, определения/ссылки variables и
вложенные списки/объекты. Если анализатор возвращает `unsupported` (например,
для пользовательской директивы), безопасный AST может быть доступен, но запрос
в покрытие не попадёт.
Не выполняется синтез из selectedFields и не расширяется Coverage Engine.

| Состояние | Точная семантика |
|---|---|
| `available` | Настоящий document успешно разобран и нормализован; scalar literals для маскирования не было. Форматирование/комментарии исходника не сохраняются |
| `redacted` | Настоящий document разобран, нормализован и все scalar literals заменены placeholders, в том числе defaults и значения directives |
| `unavailable` | Историческая запись, неподдерживаемое тело, ошибка/неизвестный AST, превышение лимитов или сбой нормализации. safeGraphqlDocument строго null |

Строки и block strings → `"[REDACTED]"`; целые → `0`; float → `0.0`; enums →
`REDACTED`; booleans → `false`; null → `null` с состоянием redacted. Контейнеры
и имена их полей сохраняются, все листья маскируются. Пустые `[]`/`{}` без
scalar leaves не требуют redaction. Имена operations/variables/aliases/полей —
структура документа и не анонимизируются; секреты не следует кодировать в именах.

Это структурная копия, **не исходный текст и не исполняемый replay**. Placeholders
могут не соответствовать enum/type constraints; не отправляйте этот текст upstream.
Значения runtime variables, токены/Authorization/cookies и response bodies
не сохраняются. Оригинальный запрос отправляется upstream без изменений.
Hash/operationKey считаются как раньше от оригинального query, а не от safe copy.
Невалидный Unicode без UTF-8 представления имеет неизвестный hash/identity,
без синтетического значения; анализ/наблюдение при этом сохраняются.

Доступ — только detail: analyst/operator **с grant**, admin либо CLI key.
Публичность сервиса не открывает журнал. Viewer/anonymous не получают документ;
чужой requestId/runId и отсутствие grant — существующие 404/403/401.
`/requests` список, `/operations` и `/stats` не содержат document полей.
Список не загружает text-column из БД, чтобы 500 строк не выделяли до 32 MiB
невидимых документов. Отзыв grant/session проверяется при каждом чтении.

### Политика хранения и ресурсы

- Только print_ast безопасной копии, без source locations, исходных комментариев
  и исключений парсера. Нет кеша/лога исходных документов обращений.
- Максимум input для safe normalization — 256 KiB UTF-8 (wire proxy сохраняет
  существующий лимит 1 MiB). До 10 000 tokens, 20 000 AST nodes и depth 64.
- Persisted text — не больше **65 536 UTF-8 bytes**, в том числе ограничение
  PostgreSQL CHECK. Превышение означает unavailable, **без усечения** и fake document.
- AST parse/normalization выполняются в worker thread; неизвестные виды AST
  запрещены. Сбой необязательного safe display не повторяет mutation и не
  обнуляет реальное наблюдение/покрытие.
- Safe copy хранится вместе с наблюдением на срок его существования. Автоматический
  TTL/фоновое удаление не реализованы; при удалении наблюдения safe copy
  удаляется как его колонка. Для sizing учитывать максимум 64 KiB на новый request;
  фактический размер зависит от структуры. Raw query/variables/ответы не архивируются.
- Исторические обращения не восстанавливаются: default unavailable/null.

## SDL, labels и граф

```http
GET /api/schemas/162ef82d-d806-4f08-b901-e08b5d9228e2/sdl
```

```json
{
  "schemaId": "162ef82d-d806-4f08-b901-e08b5d9228e2",
  "schemaHash": "<original stored SHA-256>",
  "sdl": "type Query {\n  user: User\n}\n\ntype User {\n  id: ID!\n}"
}
```

SDL — сохранённая каноническая структура выбранного snapshot, а не документ
обращения. Идентификатор/hash возвращаются из той же строки БД, без пересчёта.
Read ACL соответствует schema graph: public или viewer/analyst/operator с grant,
admin/CLI; приватный недоступный snapshot — 404. Schema SDL может содержать
объявленные author defaults/descriptions: это версия схемы, а не runtime data.
Публичность схемы необходимо выбирать с учётом её собственного содержимого.

```http
PATCH /api/schemas/162ef82d-d806-4f08-b901-e08b5d9228e2
Content-Type: application/json
X-CSRF-Token: <session csrf token>
Origin: https://graphql.rebyte.fun

{"displayName":"Release candidate"}
```

Оператор требует grant; admin/CLI может менять любой snapshot. Analyst/viewer
не редактируют. Ответ SchemaOut; displayName trim, 1–255 символов, `null` очищает.
Пустой body/blank/лишние поля — 422. Импорт/dedup возвращают существующий label
и не переименовывают snapshot. При null UI использует дату/короткий hash.
Ни snapshot ID, schemaHash/SDL, поля/relations, runs, ни покрытие не меняются.
POST SDL ограничен 1 MiB всего JSON body; PATCH — 16 KiB; превышение — 413.
Неверная/неподдерживаемая SDL — 422 с безопасным сообщением, без частичного snapshot.
Схемы ограничены 5000 типами в GraphQL type_map (включая служебные) и 50 000 полями; имена сохраняемых типов/полей ≤255.

Граф дополнен:

```typescript
type SchemaArgumentOut = {
  name: string; type: string; description: string | null;
  defaultValue: string | null;
  defaultValueAvailability: 'absent' | 'available' | 'redacted' | 'unavailable';
};
// GraphField: description, deprecationReason, args: SchemaArgumentOut[]
// GraphType: description, rootOperations: ('query'|'mutation'|'subscription')[],
//            interfaces: string[], possibleTypes: string[]
// CoverageGraph: rootTypes: {query: string|null; mutation: string|null; subscription: string|null}
// GraphRelation: id: string (UUIDv5); все прежние поля сохранены.
```

Root names берутся из schema definition, а не предполагаются Query/Mutation/
Subscription. Массив сохраняет также исторические совмещённые root assignments,
принимавшиеся graphql-core 3.2; graphql-core 3.3 строже валидирует новые imports.
Kind остаётся GraphQL kind OBJECT/INTERFACE/UNION. possibleTypes и interfaces —
настоящие отсортированные связи типов. Union не получает выдуманные поля;
поле с return interface/union указывает на объявленный abstract type, а не на
каждую возможную реализацию. Граф по-прежнему показывает OBJECT/INTERFACE/UNION;
полный SDL содержит также input/enum/scalar definitions.

Relation id = UUIDv5(namespace=snapshot UUID, name=JSON `[fromType,field,toType]`
с separators `,`/`:` и ASCII escaping). ID стабилен для того же snapshot/coordinate,
другой snapshot имеет другую identity, label не влияет. Coverage denominator
по-прежнему включает только OBJECT fields, как до расширения.

Metadata читаются из неизменяемого SDL, поэтому доступны и для старых snapshots
без backfill IDs/hash/полей. Default values аргументов показываются только через
тот же AST masker; отсутствие default — absent/null, ошибка — unavailable/null.
Schema cache один для анализа и metadata, ограничен 32 snapshots.

Рекомендуемый frontend порядок: расширить DTO → KPI статусов/P95 → labels/SDL →
args/descriptions/rootTypes/relations → read-only preview документа в protected detail.
Redacted документ маркировать как изменённый; unavailable показывать явно;
не рисовать выдуманный query и не заменять отсутствие durations нулями.
