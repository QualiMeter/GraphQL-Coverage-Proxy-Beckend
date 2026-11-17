# Test Coverage: привязка обращений к тестам

Test Coverage связывает прогон, попытку выполнения pytest/Playwright-теста,
его результат, журнал GraphQL-обращений и поля схемы. Результат теста сообщает
runner после завершения setup, тела теста и teardown. HTTP 200 не означает
`passed`: ошибка assertion или teardown сохраняется как `failed`.

## Модель данных

`TestRun → TestExecution → ObservedRequest → RequestFieldUsage / RequestFieldEvidence`.

У попытки есть UUID `id`, внешний `testId`, отображаемое `name`, `framework`
(`pytest` или `playwright`), `attempt`, статус, время, длительность и безопасный
`failureCode`. Пара `(run, framework, testId, attempt)` уникальна. Повторный запуск
после ошибки получает новую попытку и UUID; предыдущая попытка сохраняется.
Повторение теста Playwright через `repeatEach` входит в его внешний идентификатор.

`ObservedRequest.testExecutionId` допускает null: старые обращения и трафик без
SDK остаются в общем покрытии. Составной внешний ключ запрещает связывать
обращение одного прогона с попыткой другого.

## Доступ

Чтение требует роли analyst или выше и доступа к сервису; запись — operator
или admin. Для browser-сессии действуют существующие grants и CSRF. Доверенный
runner использует `X-Coverage-Api-Key`; ключ не передаётся в браузер магазина.
Публичный доступ к сервису не раскрывает имена тестов и результаты.

## Протокол runner

### 1. Зарегистрировать попытку

`POST /api/runs/{runId}/tests`

```json
{
  "id": "7ed3aab6-6dd7-426c-b219-151db7d898d5",
  "testId": "tests/test_catalog.py::test_product_name",
  "name": "Название товара в каталоге",
  "framework": "pytest",
  "attempt": 1
}
```

UUID создаётся клиентом до запроса. Повтор того же UUID и метаданных безопасен,
включая восстановление после потерянного ответа. Другие метаданные для UUID
или второй UUID для той же попытки дают 409. Новый тест регистрируется только
в работающем прогоне. `testId` ограничен 2048 символами, имя — 512, attempt —
1–10000; управляющие символы и неизвестные свойства отклоняются.

### 2. Передавать идентификаторы в GraphQL

```http
POST /graphql
X-Coverage-Run-Id: <runId>
X-Coverage-Test-Execution-Id: <executionId>
Content-Type: application/json
```

Оба служебных заголовка удаляются перед отправкой upstream. Устаревшие
`X-Test-Id` и `X-Scenario-Name` также удаляются, но не создают привязку.
Неверный или повторённый заголовок UUID даёт 400, чужая/несуществующая попытка —
404, завершённая попытка — 409. При этих ошибках upstream не вызывается.

Допуск обращения увеличивает `inFlightRequests` в короткой транзакции.
Во время сетевого ожидания соединение PostgreSQL не удерживается.
Освобождение счётчика выполняется отдельной короткой транзакцией, в том числе
при ошибке upstream. Автоматического повторения GraphQL-запроса нет.

### 3. Опубликовать окончательный результат

`POST /api/runs/{runId}/tests/{executionId}/finish`

```json
{"status":"failed","durationMs":142,"failureCode":"assertion_failed"}
```

Статусы: `running`, затем `passed`, `failed`, `skipped` или `interrupted`.
Сервер не выводит результат из HTTP-ответов. Разрешённые коды ошибки:
`test_failed`, `assertion_failed`, `setup_failed`, `teardown_failed`, `timeout`,
`runner_interrupted`. Traceback, тексты assertion и токены сюда не передаются.
Для passed/skipped failureCode отсутствует или null. Длительность — целое число
миллисекунд до 31536000000; при отсутствии сервер измеряет её по своим часам.

Результат неизменяем: повтор совпадающего finish безопасен, изменение даёт 409.
Finish разрешён после закрытия прогона, чтобы учесть ошибки teardown.
При незавершённых обращениях passed/failed/skipped дают 409 с
`detail.code = test_requests_in_flight`; runner должен дождаться обращений.
Interrupted можно сохранить сразу, а уже допущенные обращения могут закончить
запись. Новые обращения завершённой попытки отклоняются.

Если процесс runner погиб без finish, сервер сохраняет running. После закрытия
прогона возвращается `resultMissing=true`: это отсутствие результата, а не
успех. Авторизованный клиент может закрыть такую попытку как interrupted.
После аварийного завершения процесса proxy счётчик может остаться ненулевым;
он не является распределённой очередью заданий или гарантией восстановления.

## Чтение и фильтры

| Метод и путь относительно `/api/runs/{runId}` | Ответ |
| --- | --- |
| `GET /tests` | Попытки и агрегаты каждой попытки |
| `GET /tests/{executionId}` | Метаданные, результат, количество обращений и полей |
| `GET /tests/{executionId}/fields` | Поля, hits и состояния evidence |
| `GET /requests?testExecutionId={executionId}` | Существующий журнал, ограниченный попыткой |
| `GET /test-coverage` | Уникальные тесты, попытки, статусы, обращения с привязкой и без неё |

Списки поддерживают `limit` 1–500 и `offset`, общее количество — в
`X-Total-Count`. Для `/tests` доступны `status`, `framework`, `testId`,
`typeName`, `fieldName`, `fieldMode`. Для fieldName обязателен typeName.
`fieldMode=covered` ищет учтённое покрытие, `selected` — выбранное поле,
`observed` — подтверждённое поле ответа. По умолчанию используется covered.
Порядок попыток стабилен по startedAt и UUID, полей — по имени типа и поля.

## Что означает покрытие

Используется существующее правило общего покрытия: OBJECT-поля при успешном
анализе и объекте `data` в ответе. Статус failed теста не отменяет реальные
обращения к полям. Наблюдение поля не доказывает, что тест содержит assertion
для каждого его значения.

В ответе попытки отдельно указаны selectedFieldCount, coveredFieldCount,
observedFieldCount, requestCount и countedRequestCount. В строке поля:
selectedRequestCount, hits, observedCount, notObservedCount,
indeterminateCount и unavailableCount. Старые обращения без evidence считаются
unavailable, а не observed. Процент использует все OBJECT-поля снимка прогона.

## Развёртывание и совместимость

Примените `alembic upgrade head` до запуска новой версии API. Миграция
`20261124_test_executions` добавляет таблицу и nullable-связь, не переписывая
историю. Downgrade удаляет метаданные попыток и связь, сохраняя старые обращения
и покрытие. Перед миграциями рабочей БД нужен обычный backup.

До слияния используйте ветки `feat/test-coverage` backend и QA совместно.
SDK pytest, Playwright и визуальный разбор реализованы в QA. Основной frontend
может использовать описанные API для экрана «тест → обращения → поля»;
изменения его исходников в эту задачу не входят.
