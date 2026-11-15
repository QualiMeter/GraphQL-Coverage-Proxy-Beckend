# GraphQL Coverage Backend — REST API / GraphQL Proxy v0.1

Бэкенд GraphQL Coverage: PostgreSQL, REST API для публичного каталога
и администрирования, HTTP-прокси GraphQL и анализ покрытия по E2E-наблюдениям.

**Production:** [Swagger](https://graphql.rebyte.fun/docs) ·
[OpenAPI](https://graphql.rebyte.fun/openapi.json) ·
публичный API: `https://graphql.rebyte.fun/api/...`.

**Frontend:** [работающий интерфейс](https://graphql.rebyte.fun/coverage/) ·
[репозиторий React/Vite](https://github.com/QualiMeter/GraphQL-Coverage-Proxy-Frontend).
Статическая сборка публикуется на VPS отдельным GitHub Actions workflow при
успешном CI на `main`; Nginx отдаёт её с того же HTTPS origin, что и API.
Пока UI использует mock/localStorage, интеграция с реальным API — задача фронтендера.

Регистрация для просмотра опубликованных сервисов не нужна. В этой ветке добавлены
серверные сессии, роли и ACL (этап 1 v2.0, до production не выложено).
[Безопасные GraphQL error details](docs/graphql-error-details.md) ·
[Контракт авторизации и bootstrap](docs/frontend-auth.md) ·
[план следующих P0/P1](docs/v2-implementation-plan.md).
CLI/E2E сохраняет серверный API key, браузер использует cookie+CSRF.

## Что уже работает

- Семь core-таблиц PostgreSQL и четыре таблицы авторизации с внешними ключами, CHECK, UNIQUE и индексами;
  отдельная миграция для приватности сервисов.
- Импорт схемы из **SDL** и через GraphQL introspection с разбором полей и SHA-256 снимка.
- Создание сервиса и прогона E2E; запрет второго активного прогона в рамках снимка схемы.
- Сохранение наблюдения через Python-функцию `app.services.record_observation()`.
- Подсчёт уникального покрытия полей и числа запросов, затронувших каждое поле.
- REST API и автоматическая документация Swagger/OpenAPI.
- Пользователи viewer/analyst/operator/admin, серверные сессии, CSRF, grants, bootstrap CLI.
- `POST /graphql` — безопасная маршрутизация run → snapshot → service.endpointUrl.
- GraphQL AST Analyzer для query/mutation, подключённый к E2E-записи через async-адаптер.
- Публичные/приватные сервисы (`isPublic`) и API-ключ для изменяющих операций.

**Пока не реализовано:** поддержка GraphQL fragments/subscriptions,
привязка к конкретному E2E-тест-кейсу.
Публичный фронтенд никогда не должен содержать административный API-ключ.

## Локальный запуск через Docker

Нужен Docker с Compose:

```bash
docker compose up --build -d
```

Compose поднимет PostgreSQL 16, применит миграцию Alembic и запустит FastAPI.

- API: http://localhost:8000
- Swagger: http://localhost:8000/docs
- OpenAPI JSON: http://localhost:8000/openapi.json
- PostgreSQL на хосте: 127.0.0.1:5432

Учётные данные PostgreSQL в `compose.yaml` только для локальной разработки.
В реальном окружении используйте отдельные секреты.

Проверка:

```bash
curl http://localhost:8000/health
```

Ожидаемый ответ: `{"status":"ok"}`.

## Приватные и публичные сервисы

Для доверенного CLI/CI задайте `COVERAGE_API_KEY` в окружении (минимум
32 символа, рекомендуется `openssl rand -hex 32`). Браузер использует login/session
и CSRF; API key для него не нужен. Первого администратора создаёт
`python -m app.auth_cli bootstrap-admin initialadmin` после миграции.
Пароль запрашивается через getpass. Открытой регистрации нет.

- Новые и ранее созданные сервисы **приватны по умолчанию** (`isPublic=false`).
  Администратор может опубликовать сервис через
  `PATCH /api/services/{service_id}` с телом `{"isPublic":true}`.
- `GET /api/services`, списки схем/прогонов и графы покрытия без ключа доступны
  только для опубликованных сервисов. Авторизованные видят также сервисы с grant.
  Приватные UUID без разрешения дают `404`.
- Произвольное поле `metadata` тестового прогона скрыто при публичном чтении
  (`{}`); журнал `GET /api/runs/{run_id}/requests` доступен analyst/operator с grant
  либо admin/CLI. Произвольное metadata выдаётся только admin/CLI.
- Создание сервисов, импорт схем, запуск и завершение прогонов, управление
  видимостью и `POST /graphql` требуют operator/admin session+CSRF и grant
  либо доверенный `X-Coverage-Api-Key`.
  Административный ключ никогда не пересылается на upstream; собственный
  клиентский `Authorization` по-прежнему передаётся прозрачно.
- Секретный ключ допустим только на сервере или в защищённой CI-среде,
  не в React/Vite-приложении.

Публичный REST API доступен на `https://graphql.rebyte.fun` без префикса
`/coverage`. В production фронтенд обращается к API относительными путями
(`fetch('/api/services')`): CORS между ними не нужен, поскольку origin общий.
Локально Vite проксирует `/api` на сервер API.
Контракт, примеры ответов и ограничения для frontend:
[docs/frontend-api.md](docs/frontend-api.md).

## Запуск без Docker для API (PostgreSQL нужен отдельно)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
```

Переменные из `.env` нужно экспортировать в окружение (файл не загружается автоматически):

```bash
export DATABASE_URL=postgresql+asyncpg://coverage:coverage_dev@localhost:5432/coverage
export CORS_ORIGINS=http://localhost:5173
alembic upgrade head
uvicorn app.main:app --reload
```

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
  анализа, ссылки на поля и только masked AST copy. Исходные query, variables
  и ответы не сохраняются. Безопасная копия реально выполненного document выдаётся в защищённом request detail.
- Признаки GraphQL-ответа вычисляются по **декодированной копии JSON**.
  Для анализа gzip/deflate распаковываются с пределом 1 МиБ, непроверяемый ответ
  даёт `None`. Клиент получает исходные сжатые байты ответа; в БД они не сохраняются.
- При падении анализатора записывается `internal_error`, без фиктивного покрытия.

`POST /graphql` вызывает этот модуль при наличии `X-Coverage-Run-Id` и ждёт commit
перед отправкой исходного ответа. При запуске `app.state.coverage_analyzer`
получает настоящий `app.analyzer_adapter.analyze_e2e_query`: он компилирует SDL
снимка и выполняет синхронный `analyze_query()` в worker thread. Схемы кешируются
с ограничением 32 снимка. CLI proxy без заголовка работает без БД только в явном legacy-режиме.
Некорректный/неоднозначный UUID даёт 400, неизвестный прогон — 404,
завершённый — 409. Сервис без endpoint даёт 409, если legacy fallback явно не включён.

Если запись после upstream не удалась, ответ — 502 с предупреждением, что upstream
уже мог выполнить запрос. Автоматически повторять mutation нельзя.
Проверка cookie session и preflight run/snapshot/service освобождают соединения
PostgreSQL до DNS, чтения тела и upstream HTTP. После ответа наблюдение сохраняется
в новой транзакции с прежней блокировкой run относительно `finish`.
Полные имена GraphQL-операций сохраняются как `TEXT` и точно фильтруются в журнале,
включая имена длиннее 255 символов. Требуется миграция
`20261119_operation_name_text`; [проверки и ограничение downgrade](docs/stage6-audit-report.md).
Подробный контракт, порядок подключения и оставшиеся зависимости:
[docs/e2e-integration.md](docs/e2e-integration.md).

## HTTP-прокси GraphQL

Создайте отдельный сервис с обязательным `endpointUrl`, импортируйте схему через introspection или SDL и
создайте run. Прокси выбирает адрес через `X-Coverage-Run-Id → TestRun →
SchemaSnapshot → GraphQLService.endpoint_url`. Переключать глобальное окружение
при переключении сервисов не требуется. [Контракт frontend](docs/frontend-multi-service.md)
и [готовый пример двух сервисов](docs/multi-service-e2e.md).

```bash
curl http://localhost:8000/graphql \
  -H 'Content-Type: application/json' \
  -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" \
  -H "X-Coverage-Run-Id: $RUN_ID" \
  --data-binary '{"query":"query Health { health }"}'
```

Сохраняются исходные байты тела, разрешённые заголовки, статус и тело ответа.
Browser Cookie/Set-Cookie, внутренние X-Coverage-*, CSRF, forwarding и hop-by-hop
headers не пересылаются. Только доверенный CLI без Origin/Sec-Fetch-* может
явно передать upstream cookies; общего cookie jar нет.

HTTPS:443 обязателен по умолчанию. URL без credentials/query/fragment проверяется
при сохранении и перед соединением. Все DNS-ответы должны быть безопасны;
сокет закреплён за проверенным числовым IP, TLS проверяет исходный hostname.
`POST /api/services` и `PATCH /api/services/{id}` освобождают соединения БД
до DNS. Перед записью повторно проверяются cookie session, роль и grant;
PATCH делает это под блокировкой сервиса и повторно проверяет активные runs.
Redirects, environment proxies и автоматические retries выключены.
Лимиты: request 1 МиБ, response 8 МиБ, анализ decoded response 1 МиБ,
DNS 3 с, чтение входящего тела 10 с, upstream обмен 30 с, 16 обменов на worker.
HTTP/локальные адреса требуют одновременно COVERAGE_DEV_MODE=true и точный
COVERAGE_DEV_UPSTREAM_ORIGINS. Production Compose dev-режим запрещает.
[Архитектура и ограничения SSRF](docs/multi-service-security.md).

Временная совместимость: COVERAGE_LEGACY_PROXY=true разрешает доверенному
CLI без run ID использовать TARGET_GRAPHQL_URL, а старым сервисам с NULL
endpoint — fallback к нему. Новый configured-сервис всегда использует свой URL.
Browser всегда требует run ID. Legacy по умолчанию выключен.

Смена endpoint при любом running run сервиса возвращает 409. После завершения
всех runs endpoint можно изменить, snapshots/наблюдения сохраняются.
502/504 после отправки могут означать уже выполненную mutation: автоматический
повтор запрещён. Только HTTP POST; WebSocket и потоковая доставка не поддержаны.

## Импорт схемы через introspection

`POST /api/schemas/import/introspection` принимает только `{"serviceId":"<UUID>"}`,
находит сохранённый endpointUrl сервиса и выполняет стандартный introspection query.
Настроенные сервисы всегда используют свой адрес, независимо от глобального target.
Operator требует grant, admin может импортировать любой сервис. Browser использует
cookie+Origin+CSRF; URL, credentials и произвольные headers в JSON не принимаются.
Входящие cookies/CSRF/API key/Authorization не передаются upstream.

```bash
curl --fail-with-body -sS http://localhost:8000/api/schemas/import/introspection \
  -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" \
  -H 'Content-Type: application/json' -d '{"serviceId":"<service UUID>"}'
```

Ответ 201 — существующий SchemaOut. Дедупликация serviceId + schemaHash сохраняет
snapshot ID; SDL-снимок с тем же hash обогащается source/introspection metadata
без изменения исторических полей/runs/observations. Конкурентные SDL/introspection
импорты сериализованы service lock, все flush защищены savepoint; миграция не нужна.
Смена endpoint во время fetch возвращает 409 без сохранения старой схемы.

Сохраняются SSRF/DNS pinning, TLS verification, запреты redirects/retries/proxies,
лимиты 8 МиБ / 15 с на fetch и общий с proxy лимит 16 jobs/worker. Нормализация
в worker thread имеет отдельные пределы размера/сложности. Поддержан identity encoding.
Ошибки содержат безопасный строковый detail и стабильный code; отключённая
introspection — 409 INTROSPECTION_DISABLED с предложением ручного SDL.
Закрытые API с upstream-аутентификацией пока требуют SDL: токены не хранятся.
Legacy fallback для NULL endpoint — только explicit COVERAGE_LEGACY_PROXY=true.
[Frontend-контракт и таблица ошибок](docs/frontend-introspection.md),
[архитектура/ограничения](docs/service-introspection.md).

## Обращение фронтенда

Подробный контракт: [docs/frontend-api.md](docs/frontend-api.md).
Договорённость: JSON-имена — camelCase, идентификаторы — UUID, время — ISO 8601 UTC.

Порядок работы серверного администратора/CI (изменяющие методы требуют
`X-Coverage-Api-Key`):

1. `POST /api/services` — создаём запись сервиса.
2. `POST /api/schemas/import` — импортируем SDL, получаем `schemaId` (в ответе `id`).
3. `GET /api/schemas/{schemaId}/graph` — карта ещё без данных о прогонах.
4. `POST /api/runs` — запускаем прогон.
5. Позже прокси записывает наблюдения через `record_observation()`.
6. `GET /api/runs/{runId}/coverage` — получаем реальные поля, связи и покрытие.
7. `POST /api/runs/{runId}/finish` — закрываем прогон.

## Аналитика операций и прогонов

REST API предоставляет агрегаты существующих наблюдений без сохранения
исходных GraphQL-документов, переменных и токенов:

- `GET /api/runs/{runId}/operations` — группировка по `query_hash`, имени
  и типу, включая безымянные операции; счётчики, ошибки, время и поля
  `fields: [{typeName, fieldName, hits}]`. `hits` считает только учтённые
  обращения с `analysis_status=success AND response_has_data=true`, как
  Coverage Engine и `countedRequestCount`; повтор поля в обращении считается
  один раз. Из трёх успешных анализов с двумя ответами с объектом `data`
  получаем `hits=2`. Поля без учтённых обращений не включаются в `fields`.
  `requestCount` на уровне операции сохраняет общее число обращений группы.
  Необязательные фильтры: `operationName` (точное имя) и `operationType`.
- `GET /api/runs/{runId}/stats` — все обращения, уникальные операции,
  независимые HTTP/GraphQL-ошибки, покрытие существующего Coverage Engine и
  непустые интервалы по одной минуте в UTC. Неизвестные длительности
  исключены из среднего; без измерений возвращается `null`.

Для опубликованных сервисов (`isPublic=true`) агрегаты доступны без ключа;
для приватных проверка идёт через прогон → снимок → сервис (анонимно — `404`).
Точные DTO, примеры и семантика — в [контракте фронтенда](docs/frontend-api.md#81-операции-прогона)
и FastAPI OpenAPI. Завершённые прогоны используют собственные исторические данные.
Детальный журнал, `POST /graphql` и изменяющие методы
требуют авторизации с соответствующими ролью и grant. Пользователей создаёт admin,
самостоятельной регистрации нет. Не передавайте `COVERAGE_API_KEY` в браузер.
История по сервису, фильтры журнала и request detail реализованы в этапе 4;
[frontend-контракт, total/pagination/ACL и миграция](docs/frontend-history-requests.md).
Этап 5 добавляет P95/analysisStatusCounts, masked AST реального документа,
SDL и метаданные схемы, displayName. [Контракт frontend](docs/frontend-final-features.md),
[миграция и будущий release](deploy/STAGE5-RELEASE.md).
сравнение прогонов — P2, позже.

## Тесты

```bash
pip install -e '.[test]'
pytest -q
```

Интеграционный тест PostgreSQL работает только с переменной `TEST_DATABASE_URL`.
Он использует откатываемую внешнюю транзакцию и требует уже созданных таблиц:

```bash
export TEST_DATABASE_URL=postgresql+asyncpg://coverage:coverage_dev@localhost:5432/coverage
pytest -q -m integration
```

Используйте только выделенную тестовую БД. Если переменная не задана,
интеграционный тест пропускается (это не считается доказательством работы БД).

## Подключение остальных бэкендеров

- Общие типы: `app/contracts.py`.
- GraphQL Analyzer должен выдавать `AnalysisResult` со списком `FieldReference`.
- Proxy вызывает `capture_e2e_exchange()` после ответа upstream для запросов
  с runId; реальный Analyzer подключён через `app.analyzer_adapter`.
- Хеш запроса — SHA-256 текста GraphQL; не хранить переменные, авторизацию и cookies.
- `response_has_data=True` означает, что GraphQL-ответ содержит объект `data`, а не `null`;
  только такие успешно проанализированные запросы идут в процент покрытия.
- GraphQL ошибки могут сосуществовать с `data`; процент отражает выбранные поля,
  а не успешность выполнения каждого вложенного resolver.

Изменения в `contracts.py` согласовываются между разработчиками.

## Проверка этапа авторизации

```bash
export DATABASE_URL=postgresql+asyncpg://coverage:coverage_dev@localhost:5432/coverage_test
export TEST_DATABASE_URL="$DATABASE_URL"
alembic upgrade head
pytest -q tests/test_browser_auth.py tests/test_browser_auth_postgres.py tests/test_auth_migrations_postgres.py
python -m app.auth_cli cleanup
```

Только отдельная тестовая БД. Migration-тест создаёт/удаляет временную БД на том же
тестовом сервере и требует CREATEDB. PostgreSQL проверяет upgrade/downgrade с
историческими записями, роли, grants, cookie/CSRF, отзыв сессий, throttling и proxy.
Для HTTP разработки явно задать COVERAGE_DEV_MODE=true и точный
COVERAGE_PUBLIC_ORIGIN; production — HTTPS и доверенные reverse proxy headers.
Подробные настройки, эксплуатационные ограничения и DTO — [frontend-auth.md](docs/frontend-auth.md).
Деплой и production-миграции этой задачей не выполняются.

Перед merge при существующем автодеплое: [порядок безопасного выпуска PR #23](deploy/PR23-RELEASE.md).
Production требует точный FORWARDED_ALLOW_IPS после Docker NAT и использует
`python -m app.server`; wildcard/сети запрещены. Тест полного локального Docker-стенда:
`python tests/docker_auth_smoke.py` (Linux Docker daemon, отдельный project, тестовый TLS).
CI выполняет его после полного pytest; VPS при этом не используется.
