# Экспорт результатов в JSON и CSV

Экспорт формируется основным backend из сохранённых данных PostgreSQL.
QA/TestLab, работающий браузер или доступность upstream для скачивания не
требуются. Дополнительных зависимостей и миграций эта доработка не добавляет;
используется действующая схема базы после `alembic upgrade head`.

## Маршруты и права доступа

| Метод и путь | Данные | Доступ |
| --- | --- | --- |
| `GET /api/runs/{runId}/exports/coverage` | Поля снимка и hits выбранного прогона | Как `/coverage`: публичный сервис либо доступ к закрытому |
| `GET /api/runs/{runId}/exports/operations` | Агрегаты операций с полями | Как `/operations` |
| `GET /api/runs/{runId}/exports/requests` | Безопасные метаданные обращений | Analyst/operator с grant, admin или CLI |
| `GET /api/runs/{runId}/exports/tests` | Попытки тестов, результаты и показатели покрытия | Как `/tests`: analyst/operator с grant, admin или CLI |
| `GET /api/runs/{runId}/exports/test-matrix` | Связь попытки теста с полем и счётчиками | Те же права, что у Test Coverage |
| `POST /api/runs/compare/export` | Поля сравнения, метрики, дельты и типы | Как `/api/runs/compare`; проверяется каждый прогон |

У всех маршрутов параметр `format=json` или `format=csv`, по умолчанию JSON.
Браузер использует действующую Coverage-сессию; для POST нужны корректные
`Origin` и `X-CSRF-Token`. CLI использует `X-Coverage-Api-Key`. Публичность
сервиса не открывает выгрузки обращений, тестов и матрицы.

Файл имеет `Content-Disposition: attachment` с именем
`<dataset>-<run UUID>.<format>`. Для сравнения в имени используется baseline.
Ответ содержит `Content-Length`, `X-Export-Format-Version: 1`,
`X-Total-Count` с количеством строк данных и `Cache-Control: no-store`.
Заголовки имени файла и версии доступны через CORS при разрешённом origin.

Пример скачивания публичного прогона в PowerShell; замените UUID:

```powershell
$baseUrl = 'http://localhost:8000'
$runId = '33333333-3333-4333-8333-333333333333'
New-Item -ItemType Directory -Force reports/exports | Out-Null
Invoke-WebRequest "$baseUrl/api/runs/$runId/exports/coverage?format=csv&covered=false" `
  -OutFile reports/exports/uncovered.csv
```

Для защищённой выгрузки доверенным CLI добавьте
`-Headers @{'X-Coverage-Api-Key' = $env:COVERAGE_API_KEY}`. Значение ключа не
передавайте в URL и не сохраняйте рядом с отчётом. Папка `reports/` исключена
из Git. Также маршруты доступны в Swagger `/docs`, раздел Exports.

## Фильтры и порядок

Фильтры применяются до выгрузки. Страницы таблицы не ограничивают файл:
`limit` и `offset` у выгрузок requests/tests/test-matrix принимаются для
совместимости с параметрами таблиц, но игнорируются и не включаются в
метаданные. Записи не обрезаются молча. Неподдерживаемый фильтр возвращает `422`.

| Выгрузка | Фильтры |
| --- | --- |
| Coverage | `typeName`, `fieldName`, `covered`, `search`, `minHits`, `maxHits`, `returns=scalar/object/list`, `deprecated`; `fieldName` требует `typeName` |
| Operations | `operationName`, `operationType`, `search`, `typeName`, `fieldName`, `hasErrors`, `sortBy=identity/count/duration/errors`, `sortOrder=asc/desc` |
| Requests | Все параметры `RequestFilters`: тест, операция, тип/поле, ошибки, HTTP-статус, период и сортировка; дополнительно `errorGroup=http/analyzer` |
| Tests | `status`, `framework`, `testId`, `typeName`, `fieldName`, `fieldMode=covered/selected/observed`, `teamKey`, `suiteKey`, `unassignedTeam`, `unassignedSuite` |
| Test matrix | Те же фильтры тестов; при заданном типе/поле строки матрицы также ограничены этими координатами |
| Comparison | Тело `RunComparisonRequest`, включая `fields.filter`; пагинация `fields` и `types` игнорируется |

Coverage сортируется по `sortBy=coordinate`, `fieldName` или `hits` с
`sortOrder=asc/desc`; по умолчанию координаты по возрастанию. Координаты
разрешают равенство hits. Requests сохраняет порядок исходного журнала,
включая NULL-значения длительности в конце и UUID при равенстве ключей.
Тесты упорядочены по времени начала и UUID, матрица дополнительно по типу
и полю. Operations по умолчанию (`identity`, `asc`) сохраняет порядок исходного API; можно
выбрать сортировку по числу обращений, средней длительности или сумме ошибок.
Неизвестные длительности идут в конце. Comparison сохраняет порядок исходного API.

`search` у Coverage ищет буквальную подстроку в типе, имени, координате и
сигнатуре поля без учёта регистра; `%` и `_` не становятся SQL-шаблонами.
У Operations поиск выполняется по отображаемому имени, включая «Без имени».
`errorGroup=http` соответствует объединению 4xx/5xx, `analyzer` — parse,
validation и internal errors. Дополнительные параметры объединяются через AND.

Coverage содержит поля `OBJECT` и `INTERFACE`, как карта схемы. Поле
`countedInDenominator` указывает участие в знаменателе: только `OBJECT`.
`summary` описывает весь выбранный прогон, а `rows` и `rowCount` — выборку
после фильтров. Выборка непокрытых полей не превращает общее покрытие в 0%.
Hits используют прежнее правило: успешный анализ и ответ с объектом `data`.

Test matrix содержит одну строку на попытку теста и выбранное ею поле.
Повторные попытки остаются отдельными записями. Выбранные поля, hits и
наблюдавшиеся поля имеют те же значения и смысл, что у
`/tests/{executionId}/fields`; неопределённость не подменяется нулевым покрытием.
Тесты без выбранных полей доступны в выгрузке tests и не создают фиктивных
строк матрицы.

## Формат JSON версии 1

```json
{
  "formatVersion": 1,
  "dataset": "coverage",
  "metadata": {
    "generatedAt": "2026-11-18T12:00:00+00:00",
    "service": {
      "id": "11111111-1111-4111-8111-111111111111",
      "key": "shop",
      "name": "Магазин"
    },
    "snapshot": {
      "id": "22222222-2222-4222-8222-222222222222",
      "schemaHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    },
    "runs": [{
      "id": "33333333-3333-4333-8333-333333333333",
      "name": "Smoke",
      "status": "completed",
      "startedAt": "2026-11-18T11:00:00+00:00",
      "finishedAt": "2026-11-18T11:01:00+00:00"
    }]
  },
  "filters": {"covered": false, "sortBy": "coordinate", "sortOrder": "asc"},
  "summary": {"totalFields": 2, "coveredFields": 1, "coveragePercent": 50.0},
  "rowCount": 1,
  "rows": [{
    "typeName": "User", "kind": "OBJECT", "fieldName": "email",
    "returnType": "String", "hits": 0, "covered": false,
    "countedInDenominator": true, "deprecated": false, "returnKind": "scalar"
  }]
}
```

Идентификаторы примера иллюстративны. Формальный контракт общего документа
доступен как `ExportDocument` в `/openapi.json` и в
[JSON Schema версии 1](export-format-v1.schema.json).

Строки operations соответствуют `OperationOut`, requests — `RequestOut`,
tests — `ExecutionOut`. В матрице к `ExecutionFieldOut` добавлены
`executionId`, `testId`, `name`, `framework`, `attempt`, `status`.
Дополнительно передаются `teamKey` и `suiteKey`; у неразмеченных попыток
они равны `null` в JSON и пустой ячейке в CSV. Фильтры команд/наборов
используют те же правила, что [Team/Suite Coverage](team-suite-coverage.md).
У comparison строки соответствуют `ComparedFieldOut`; `summary` содержит
`baselineRunId`, `targetRunId`, метрики `runs`, `comparison`, `fieldCategories`
и все `types`. Проценты, дельты и категории рассчитывает существующий механизм
сравнения. Допускаются только завершённые сопоставимые прогоны одного снимка.

Пустая выборка — корректный документ с `rowCount: 0`, `rows: []` и метаданными.
Начатый прогон тоже можно экспортировать: статус и время формирования
сохраняются в файле. Последующие обращения могут изменить следующий экспорт.

## Формат CSV версии 1

CSV использует UTF-8 с BOM, запятую, CRLF и двойные кавычки вокруг ячеек.
Это сохраняет русские имена, переносы строк и разделители внутри значений.

Первая строка после заголовка имеет `recordType=metadata`. Она содержит
версию, dataset, время, сервис, снимок, список прогонов, фильтры, summary и
rowCount. Остальные строки имеют `recordType=data` и столбцы выбранного
набора. Метаданные в них не дублируются. Даже при 0 данных остаются заголовок
и запись metadata. Для обработки в pandas/Excel отберите `recordType=data`.

Вложенные значения (`fields` операций, `hitsByRun`, summary и список прогонов)
записываются компактным JSON в одной CSV-ячейке. `null` в данных становится
пустой ячейкой, boolean — `true/false`; числа остаются числами.

Потенциальные формулы в строковых значениях получают префикс табуляции внутри
кавычек, включая варианты с пробелами, управляющими символами и полноширинными
`＝＋－＠`. Отрицательные числовые показатели не экранируются. Эта обработка
предназначена для открытия в Excel; табуляция сохраняется при программном
чтении CSV. JSON содержит исходные строки без такого преобразования.
Основание — [рекомендации OWASP для CSV](https://community.owasp.org/attacks/CSV_Injection).
Повторное редактирование и пересохранение файла в сторонней программе может
изменить правила обработки ячеек.

## Размер, согласованность и хранение

Одна выгрузка ограничена 100000 строками и 32 MiB итогового файла. До загрузки
больших выборок проверяется также оценка объёма выбранных данных в 32 MiB.
Для operations ограничивается и число/объём связей операция–поле; для
comparison — 100000 ячеек «поле × прогон». При превышении нужно сузить
фильтры или выбрать меньше прогонов. Усечения файла с успешным `200` нет.

Данные и права доступа читаются в `REPEATABLE READ, READ ONLY`, поэтому
параллельная запись трафика не смешивает разные состояния одного отчёта.
Файл сначала готовится целиком, затем передаётся частями по 64 KiB.
Транзакция и соединение PostgreSQL освобождаются до скачивания. Файл до
1 MiB хранится в памяти; больший временно переносится в системную временную
папку и закрывается после выдачи. Эти файлы не помещаются в Git-репозиторий.
Экспорт не создаёт постоянного архива и не изменяет результаты прогонов.

Подготовка ограничена 60 секундами, SQL-запрос — 30 секундами. При ошибке
до начала скачивания временный файл закрывается, API возвращает ошибку.
Raw responses, AST, значения переменных, GraphQL error payloads, cookies,
токены, endpoint и произвольный `run.metadata` в выгрузки не включаются.

## Подключение кнопки на фронтенде

Для GET можно использовать ссылку на соответствующий маршрут. Для обработки
ошибок до скачивания удобнее `fetch` с `credentials: 'same-origin'`: проверить
`response.ok`, получить Blob, скачать через `URL.createObjectURL` и после
скачивания вызвать `URL.revokeObjectURL`. Базовый URL и префикс API следует
брать из действующего клиента приложения, не задавать отдельный host.

Передавайте серверные фильтры текущей таблицы через `URLSearchParams`.
Не копируйте в файл только загруженную страницу и не добавляйте локальные
фильтры, которых нет в API: для них нужна явная договорённость с backend.
Кнопку tests/matrix/requests показывайте по тем же правам, что исходную таблицу.
Скачивание comparison выполняйте POST с текущим выбором прогонов, сессией,
Origin и CSRF. Имя файла берите из `Content-Disposition` либо составляйте
из UUID и выбранного формата.

Соответствия фильтров существующей таблицы Coverage: `covered/uncovered` →
`covered=true/false`; диапазоны Hits `zero/low/medium/high` → `0..0`, `1..5`,
`6..20`, `21..` через `minHits/maxHits`; `deprecated=yes/no` → boolean;
`returns` передаётся как есть, а `all` опускается. Сортировка `date` →
`sortBy=fieldName`, `type-field` → `coordinate`, `hits-asc` → `hits&sortOrder=asc`,
`hits` → `hits&sortOrder=desc`. Состав полей совпадает при тех же фильтрах;
порядок строковых имён может отличаться от браузерного `localeCompare` из-за
локали PostgreSQL.

| Код | Действие интерфейса |
| --- | --- |
| `401` | Предложить авторизацию |
| `403` | Проверить роль и CSRF; не повторять автоматически |
| `404` | Прогон отсутствует или недоступен |
| `409` | Выбрать завершённые прогоны одного снимка |
| `413` | Сузить фильтры или выбрать меньше прогонов |
| `422` | Исправить параметры; повтор с теми же данными не поможет |
| `503` | Сообщить о временной ошибке подготовки |

## Проверка

Используйте отдельную тестовую PostgreSQL с правом создания временных баз.
Не направляйте `TEST_DATABASE_URL` в рабочую базу.

```bash
DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head
python -m pytest tests/test_export_files.py tests/test_exports_postgres.py -q
```

Тесты проверяют все форматы и наборы, совпадение с исходными API, пустые
выборки, Unicode, опасные CSV-ячейки, записи за пределами страницы, роли/grants,
CSRF, согласованность при параллельной записи, лимиты, временные файлы и
освобождение соединения до передачи файла. В интеграционных тестах используются
реальная PostgreSQL и обращения через proxy с контролируемым upstream.
