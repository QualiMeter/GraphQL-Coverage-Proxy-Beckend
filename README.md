# GraphQL Coverage Backend — DB/API foundation v0.1

Основа бэкенда хакатона Zunami: PostgreSQL-модель, первая миграция, реальные REST-методы
для фронтенда и внутренний контракт для будущих модулей HTTP Proxy / GraphQL Analyzer.

## Что уже работает

- Семь таблиц PostgreSQL с внешними ключами, CHECK, UNIQUE и индексами.
- Импорт схемы из **SDL** с разбором полей через \`graphql-core\` и SHA-256 снимка.
- Создание сервиса и прогона E2E; запрет второго активного прогона в рамках снимка схемы.
- Сохранение наблюдения через Python-функцию \`app.services.record_observation()\`.
- Подсчёт уникального покрытия полей и числа запросов, затронувших каждое поле.
- REST API и автоматическая документация Swagger/OpenAPI.

**Пока не реализовано:** сам \`/graphql\` HTTP Proxy, интроспекция удалённого GraphQL API,
AST-анализ запросов, привязка к конкретному E2E-тест-кейсу, авторизация администраторов.
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
- Proxy должен вызывать \`record_observation(session, RequestObservationInput(...))\`
  после получения ответа upstream.
- Хеш запроса — SHA-256 текста GraphQL; не хранить переменные, авторизацию и cookies.
- \`response_has_data=True\` означает, что в GraphQL-ответе есть ключ \`data\`;
  только такие успешно проанализированные запросы идут в процент покрытия.
- GraphQL ошибки могут сосуществовать с \`data\`; процент отражает выбранные поля,
  а не успешность выполнения каждого вложенного resolver.

Изменения в \`contracts.py\` согласовываются между разработчиками.
