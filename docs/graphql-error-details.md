# Безопасные GraphQL error details

`GET /api/runs/{runId}/requests/{requestId}` дополнен тремя полями.
Старые поля сохраняются. В списке `RequestOut`, `/operations` и `/stats`
новые подробности отсутствуют.

| Поле | Значение |
|---|---|
| `graphqlErrorCount` | Фактическое число объектов в upstream `errors` до ограничения списка; `null`, если число неизвестно. Это число ошибок одного обращения, а не число обращений с ошибками из агрегатов. |
| `graphqlErrorDetailsAvailability` | `sanitized`, `truncated` или `unavailable`. |
| `graphqlErrorDetails` | Ограниченный список очищенных объектов; `null`, если подробности недоступны. |

Объект детали содержит `code: string|null`, `path: (string|integer)[]|null`,
`locations: {line: integer, column: integer}[]`, `safeMessage: string|null`.
`sanitized` означает, что очистка завершена, но не гарантирует наличие каждого
необязательного поля. `truncated` означает, что сработал лимит списка, пути,
позиций или общего JSON. `unavailable` означает отсутствие достоверных деталей.

Исторические обращения всегда получают `null` / `unavailable` / `null`, даже
если прежний `graphqlHasErrors` равен `false`. Для новых разобранных ответов
отсутствующий `errors` или `errors: []` означает подтверждённые `0` / `sanitized` / `[]`.
Неверная структура `errors` (включая `null` и массив с не-объектами) даёт неизвестное
число. Не-JSON, не-объект JSON, неподдерживаемое/невалидное сжатие и превышение
лимита анализа также оставляют подробности неизвестными. Прежние флаги сохраняют
свою семантику; truthiness некорректного `errors` не доказывает известное число.
Если очистка деталей неожиданно отказала после подсчёта, число может оставаться
известным при `unavailable` и `graphqlErrorDetails: null`.

HTTP-статус сам по себе не определяет наличие GraphQL-ошибок. Частичные `data`
могут сочетаться с `errors`. `analysisStatus` / `errorCode` описывают локальный
анализ запроса, новые поля — ошибки upstream. Ошибки upstream не создают события
внутренней диагностики PR #30. Критерии coverage и агрегаты не изменены.

## Примеры новых полей

Без ошибок:

```json
{"graphqlHasErrors":false,"graphqlErrorCount":0,"graphqlErrorDetailsAvailability":"sanitized","graphqlErrorDetails":[]}
```

Очищенная ошибка (путь подтверждён выбранной схемой и документом):

```json
{
  "graphqlHasErrors": true,
  "graphqlErrorCount": 1,
  "graphqlErrorDetailsAvailability": "sanitized",
  "graphqlErrorDetails": [{
    "code": "GRAPHQL_VALIDATION_FAILED",
    "path": ["customer", "orders"],
    "locations": [{"line":3,"column":5}],
    "safeMessage": "The GraphQL document failed validation."
  }]
}
```

Ограниченные позиции (для краткого примера список содержит одну деталь):

```json
{
  "graphqlHasErrors": true,
  "graphqlErrorCount": 1,
  "graphqlErrorDetailsAvailability": "truncated",
  "graphqlErrorDetails": [{
    "code": null,
    "path": null,
    "locations": [
      {"line":1,"column":1}, {"line":2,"column":1},
      {"line":3,"column":1}, {"line":4,"column":1},
      {"line":5,"column":1}, {"line":6,"column":1},
      {"line":7,"column":1}, {"line":8,"column":1}
    ],
    "safeMessage": null
  }]
}
```

Этот upstream прислал более восьми позиций. При 33 ошибках `graphqlErrorCount`
равен 33, в `graphqlErrorDetails` сохраняется максимум 20 объектов; состояние
также `truncated`. Ограничение размера JSON может уменьшить список ещё сильнее.

Историческое обращение:

```json
{"graphqlHasErrors":true,"graphqlErrorCount":null,"graphqlErrorDetailsAvailability":"unavailable","graphqlErrorDetails":null}
```

## Политика очистки и хранения

Исходные `message`, объект `extensions`, response body и вложенные исключения
не сохраняются. `code` допускает только `GRAPHQL_PARSE_FAILED`,
`GRAPHQL_VALIDATION_FAILED`, `BAD_USER_INPUT`, `UNAUTHENTICATED`, `FORBIDDEN`,
`PERSISTED_QUERY_NOT_FOUND`, `PERSISTED_QUERY_NOT_SUPPORTED`, `INTERNAL_SERVER_ERROR`.
Неизвестные коды становятся `null`. `safeMessage` берётся исключительно из
фиксированного серверного словаря по этому коду. **`safeMessage: null` — ожидаемое
безопасное состояние**, UI может показать собственную общую подпись.

Путь проверяется по SDL immutable snapshot этого run и выбранной операции
исходного запроса. Каждая строка должна соответствовать однозначно выбранному
полю соответствующего типа без alias. Неизвестные поля, aliases (даже совпадающие
с именами схемы), неоднозначные дубликаты, fragments и ветви, безопасность которых
не доказана, скрывают весь путь (`null`). Числовой индекс разрешён только на месте
GraphQL list. Это сознательная консервативная политика: некоторые валидные пути
будут скрыты. Наличие имени в другой схеме или другом типе недостаточно.

Лимиты ограничивают объём записи и работу с недоверенным ответом:

- 20 деталей: достаточно для интерфейса, при этом полный известный count сохраняется.
- 16 сегментов пути, строки до 64 символов, индексы `0..1_000_000` (bool запрещён).
- 8 позиций на ошибку; только целочисленные `line` / `column` в `1..1_000_000`.
  Неверные позиции отбрасываются. Произвольные строки не обрезаются и не сохраняются.
- JSON деталей — максимум 16 KiB; общий лимит действует дополнительно к количественным.
- Анализ только существующей декодированной копии до 1 MiB; identity/gzip/deflate.
  Для проверки пути документ ограничен 256 KiB и 10 000 tokens.

PostgreSQL: nullable `graphql_error_count` (INTEGER),
`graphql_error_details` (JSONB, SQL NULL для неизвестного), обязательный
`graphql_error_details_availability` (VARCHAR, default `unavailable`). Additive
миграция не восстанавливает отсутствующие исторические сведения. CHECK constraints
проверяют count, состояния, согласованность списка/флага/count, 20 объектов и 16 KiB.
Для известного count верхняя граница 1 048 576 заведомо покрывает любой массив
объектов внутри анализируемого ответа до 1 MiB.
Запись выполняется в существующей транзакции observation/field usage. Обработка
ошибок не повторяет upstream, не изменяет его ответ и не записывает raw bodies.

## Доступ

Anonymous — 401. Viewer — 403. Analyst/operator — действующий service grant,
включая публичные сервисы; после отзыва grant — 404. Admin/доверенный CLI сохраняют
свои полномочия. Чужой request в доступном run и недоступные ресурсы — 404.
Публикация сервиса не делает журнал общедоступным. Действующие cookie, CSRF,
Origin, SSRF и правила передачи заголовков сохранены.
