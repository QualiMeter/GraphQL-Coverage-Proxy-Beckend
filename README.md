# GraphQL Coverage Backend — DB/API foundation v0.1

Основа бэкенда хакатона Zunami: PostgreSQL-модель, первая миграция, реальные REST-методы
для фронтенда и внутренний контракт для будущих модулей HTTP Proxy / GraphQL Analyzer.

## Что уже работает

- Семь таблиц PostgreSQL с внешними ключами, CHECK, UNIQUE и индексами.
- Импорт схемы из **SDL** и через GraphQL introspection с разбором полей и SHA-256 снимка.
- Создание сервиса и прогона E2E; запрет второго активного прогона в рамках снимка схемы.
- Сохранение наблюдения через Python-функцию \`app.services.record_observation()\`.
- Подсчёт уникального покрытия полей и числа запросов, затронувших каждое поле.
- REST API и автоматическая документация Swagger/OpenAPI.
- `POST /graphql` — HTTP-прокси на адрес из `TARGET_GRAPHQL_URL`.

**Пока не реализовано:** настоящий AST-анализ запросов,
привязка к конкретному E2E-тест-кейсу, авторизация администраторов.
Эти компоненты принадлежат отдельным задачам команды. Не выставляйте этот прототип
как общедоступный сервис без ограничения доступа.

## Локальный запуск через Docker

Нужен Docker с Compose:

\`\`\`bash
docker compose up --build -d
\`\`\`

Compose поднимет PostgreSQL 16, применит миграцию Alembic и запустит FastAPI.

- API: http://localhost:8000
- Swagger: http://localhost:8000/docs
- OpenAPI JSON: http://localhost:8000/openapi.json
- PostgreSQL на хосте: 127.0.0.1:5432

Учётные данные PostgreSQL в \`compose.yaml\` только для локальной разработки.
В реальном окружении используйте отдельные секреты.

Проверка:

\`\`\`bash
curl http://localhost:8000/health
\`\`\`

Ожидаемый ответ: \`{"status":"ok"}\`.

## Запуск без Docker для API (PostgreSQL нужен отдельно)

\`\`\`bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
\`\`\`

Переменные из \`.env\` нужно экспортировать в окружение (файл не загружается автоматически):

\`\`\`bash
export DATABASE_URL=postgresql+asyncpg://coverage:coverage_dev@localhost:5432/coverage
export CORS_ORIGINS=http://localhost:5173
alembic upgrade head
uvicorn app.main:app --reload
\`\`\`

## Интеграция E2E-наблюдений

Внутренний модуль `app.e2e_capture` связывает один GraphQL HTTP-обмен
с существующим `running`-прогоном и его snapshot схемы:
`capture_e2e_exchange(session, run_id=..., request_body=...,
decoded_response_body=..., http_status=..., duration_ms=..., analyzer=...)`.

- `analyzer` обязателен: это асинхронная функция, принимающая
  `(GraphQLQueryInput, schema_sdl)` и возвращающая настоящий `AnalysisResult`.
- Поддерживается одиночный JSON GraphQL-запрос, включая `operationName` и
  `variables`. Неподдерживаемая форма сохраняется как `unsupported` без покрытия.
- В PostgreSQL сохраняются только метаданные, хеш текста операции, статусы
  анализа и ссылки на поля из схемы. Ни query, ни variables, ни ответы не сохраняются.
- Признаки GraphQL-ответа вычисляются по **декодированной копии JSON**.
  Для анализа gzip/deflate распаковываются с пределом 1 МиБ, непроверяемый ответ
  даёт `None`. Исходные сжатые байты ответа сохраняются.
- При падении анализатора записывается `internal_error`, без фиктивного покрытия.

`POST /graphql` вызывает этот модуль при наличии `X-Coverage-Run-Id` и ждёт commit
перед отправкой исходного ответа. Подключение настоящего async Analyzer — через
`app.state.coverage_analyzer` при запуске приложения. В сборке его пока нет:
по умолчанию запросы с runId дают 503 до upstream. Обычный прокси без заголовка
работает без обращения к БД. Некорректный/неоднозначный UUID даёт 400, неизвестный
прогон — 404, завершённый — 409 (при подключённом анализаторе).

Если запись после upstream не удалась, ответ — 502 с предупреждением, что upstream
уже мог выполнить запрос. Автоматически повторять mutation нельзя.
Подробный контракт, порядок подключения и оставшиеся зависимости:
[docs/e2e-integration.md](docs/e2e-integration.md).

## HTTP-прокси GraphQL

Задайте `TARGET_GRAPHQL_URL` в окружении процесса API или в `.env` для Compose
(Compose передаёт эту переменную контейнеру):

```bash
export TARGET_GRAPHQL_URL=https://your-api.example/graphql
curl http://localhost:8000/graphql \
  -H 'Content-Type: application/json' \
  --data-binary '{"query":"query Health { health }","variables":{},"operationName":"Health"}'
```

Прокси передаёт исходные байты тела, `Authorization`, `Cookie` и применимые заголовки.
Hop-by-hop заголовки, указанные в `Connection` заголовки и `X-Coverage-Run-Id`
исключаются; `Host` и `Content-Length` формируются заново. Статус и тело upstream,
включая 4xx/5xx, сжатие и отдельные `Set-Cookie`, сохраняются. Редиректы не выполняются.
Адрес upstream задаётся только окружением, HTTP-запрос не может его переопределить.

При отсутствии или некорректном `TARGET_GRAPHQL_URL` только `/graphql` возвращает
503; остальные REST API доступны. Сетевые ошибки дают 502, таймаут — 504 без
внутренних подробностей. Таймаут каждой сетевой операции — 30 секунд.
Тела запросов, переменные и секреты не сохраняются. При opt-in E2E-записи
применяется дополнительный контракт ошибок, описанный выше.

Ограничения этапа: только HTTP POST; тела запросов и ответов буферизуются в памяти,
потоковая доставка и WebSocket-подписки не поддержаны.

## Импорт схемы через introspection

Укажите GraphQL endpoint только в серверной переменной \`TARGET_GRAPHQL_URL\`.
Для импорта схемы существующего сервиса:

```bash
curl -i http://localhost:8000/api/schemas/import/introspection \
  -H 'Content-Type: application/json' \
  -d '{"serviceId":"<UUID созданного сервиса>"}'
```

Ответ — \`201 Created\` и \`SchemaOut\` с \`source: "introspection"\`.
Повторный импорт идентичной схемы использует существующий snapshot.
Адрес upstream нельзя передать в JSON (все лишние поля отвергаются с 422).
Отсутствующая или некорректная конфигурация даёт 503, недоступный upstream
или невалидная introspection-схема — 502, неизвестный serviceId — 404.

Collector пока не передаёт пользовательские Bearer-токены на upstream.
Этот маршрут, как и остальные административные операции, **не имеет авторизации**:
до публичного открытия API необходимо ограничить доступ и добавить авторизацию.

## Обращение фронтенда

Подробный контракт: [docs/frontend-api.md](docs/frontend-api.md).
Договорённость: JSON-имена — camelCase, идентификаторы — UUID, время — ISO 8601 UTC.

Порядок работы:

1. \`POST /api/services\` — создаём запись сервиса.
2. \`POST /api/schemas/import\` — импортируем SDL, получаем \`schemaId\` (в ответе \`id\`).
3. \`GET /api/schemas/{schemaId}/graph\` — карта ещё без данных о прогонах.
4. \`POST /api/runs\` — запускаем прогон.
5. Позже прокси записывает наблюдения через \`record_observation()\`.
6. \`GET /api/runs/{runId}/coverage\` — получаем реальные поля, связи и покрытие.
7. \`POST /api/runs/{runId}/finish\` — закрываем прогон.

## Тесты

\`\`\`bash
pip install -e '.[test]'
pytest -q
\`\`\`

Интеграционный тест PostgreSQL работает только с переменной \`TEST_DATABASE_URL\`.
Он использует откатываемую внешнюю транзакцию и требует уже созданных таблиц:

\`\`\`bash
export TEST_DATABASE_URL=postgresql+asyncpg://coverage:coverage_dev@localhost:5432/coverage
pytest -q -m integration
\`\`\`

Используйте только выделенную тестовую БД. Если переменная не задана,
интеграционный тест пропускается (это не считается доказательством работы БД).

## Подключение остальных бэкендеров

- Общие типы: \`app/contracts.py\`.
- GraphQL Analyzer должен выдавать \`AnalysisResult\` со списком \`FieldReference\`.
- Proxy уже вызывает `capture_e2e_exchange()` после ответа upstream для запросов
  с runId; остаётся подключить настоящую реализацию Analyzer к состоянию приложения.
- Хеш запроса — SHA-256 текста GraphQL; не хранить переменные, авторизацию и cookies.
- \`response_has_data=True\` означает, что в GraphQL-ответе есть ключ \`data\`;
  только такие успешно проанализированные запросы идут в процент покрытия.
- GraphQL ошибки могут сосуществовать с \`data\`; процент отражает выбранные поля,
  а не успешность выполнения каждого вложенного resolver.

Изменения в \`contracts.py\` согласовываются между разработчиками.
