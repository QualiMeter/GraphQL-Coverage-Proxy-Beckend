# GraphQL Coverage Backend

FastAPI backend для измерения покрытия GraphQL-схемы по реальному E2E-трафику.
HTTP-прокси направляет запросы в выбранный сервис, анализирует операции по
снимку его схемы и сохраняет наблюдения в PostgreSQL.

## Возможности

- Несколько сервисов с отдельными endpoint, импорт SDL и introspection,
  неизменяемые снимки схем, названия снимков и выгрузка SDL.
- Прогоны, история и журнал с фильтрами, покрытие полей, агрегаты операций,
  среднее время, P95 и временная шкала.
- Browser-сессии, роли viewer/analyst/operator/admin, grants и CSRF;
  отдельный административный ключ для доверенных CLI/CI.
- Защищённый request detail: выбранные поля, AST с маскированием литералов
  и ограниченные очищенные сведения об upstream GraphQL-ошибках.
- Swagger `/docs`, OpenAPI `/openapi.json`, `/health` для проверки процесса.

Поддерживаются HTTP POST и анализ query/mutation с именованными и inline
фрагментами, включая интерфейсы, union и директивы `@skip`/`@include`.
Subscriptions и нестандартные execution directives не дают покрытия; batching, WebSocket и
потоковая доставка не поддерживаются. Привязки обращения к E2E test case нет.

## Архитектура

`Service → SchemaSnapshot → TestRun → POST /graphql → upstream → observation → coverage`.
Заголовок `X-Coverage-Run-Id` определяет снимок и `service.endpointUrl`.
На одном снимке допускается один активный прогон; завершать его следует после
ожидания всех запросов. Смена endpoint запрещена при активном прогоне сервиса.

Прокси возвращает исходные статус и байты upstream после commit наблюдения.
Запись и завершение сериализованы блокировкой run. Если запись после upstream
не удалась, клиент получает безопасный 502: mutation уже могла выполниться,
**автоматически повторять её нельзя**.

Покрытие учитывает уникальные поля `OBJECT` при
`analysis_status=success AND response_has_data=true`. `data` должен быть
JSON-объектом; HTTP/GraphQL-ошибки при наличии data не отменяют учёт. Повтор поля
в одном обращении не увеличивает hits, повторное обращение увеличивает.

Основные модули: [API](app/api.py), [proxy](app/proxy.py),
[анализатор](app/query_analyzer.py), [запись наблюдений](app/e2e_capture.py),
[хранение и покрытие](app/services.py), [журнал](app/journal.py),
[аналитика](app/run_analytics.py), [outbound policy](app/upstream.py).

## Запуск

Нужны Python 3.12+ и PostgreSQL 16 либо Docker с Compose.

```bash
cp .env.example .env
# При локальном HTTP login задайте в .env:
# COVERAGE_DEV_MODE=true
# COVERAGE_PUBLIC_ORIGIN=http://localhost:8000
# Для Vite proxy используйте origin UI, например http://localhost:5173.
docker compose up --build -d
curl --fail-with-body http://localhost:8000/health
```

Dev Compose поднимает PostgreSQL на `127.0.0.1:5432`, применяет Alembic и
публикует API на `127.0.0.1:8000`. Пароль в нём предназначен для разработки.
Ожидаемый health response — `{"status":"ok"}`; доступ к БД проверяйте отдельно
через `GET /api/services`.

Без Docker для API, с отдельно запущенной PostgreSQL:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
export DATABASE_URL=postgresql+asyncpg://coverage:coverage_dev@localhost:5432/coverage
export COVERAGE_DEV_MODE=true
export COVERAGE_PUBLIC_ORIGIN=http://localhost:8000
alembic upgrade head
uvicorn app.main:app --reload
```

При запуске Python напрямую `.env` автоматически не загружается: экспортируйте
нужные переменные. После миграций создайте первого администратора интерактивно:
`python -m app.auth_cli bootstrap-admin initialadmin` (в Docker —
`docker compose exec api python -m app.auth_cli bootstrap-admin initialadmin`).
Пароль вводится через getpass. Самостоятельной регистрации нет.

## Конфигурация

Примеры: [.env.example](.env.example), [deploy/.env.example](deploy/.env.example).

| Переменная | Назначение |
|---|---|
| `DATABASE_URL` | PostgreSQL DSN с драйвером `postgresql+asyncpg` |
| `COVERAGE_API_KEY` | CLI/CI admin key: минимум 32 ASCII-символа; хранить только на сервере |
| `COVERAGE_PUBLIC_ORIGIN` | Точный browser origin без пути и завершающего `/`; default `https://graphql.rebyte.fun` |
| `COVERAGE_DEV_MODE` | Явное разрешение HTTP browser login и dev upstream policy; default false |
| `COVERAGE_DEV_UPSTREAM_ORIGINS` | Точные origins через запятую; действуют только вместе с dev mode |
| `CORS_ORIGINS` | Отдельные разрешённые origins; default пусто, cookie CORS выключен |
| `COVERAGE_LEGACY_PROXY` | Временный CLI passthrough без run ID и fallback для старых NULL endpoint; default false |
| `TARGET_GRAPHQL_URL` | Адрес только для явно включённого legacy-режима |
| `FORWARDED_ALLOW_IPS` | Для `app.server`: обязательные точные IP непосредственного reverse proxy peer |
| `API_ROOT_PATH` | Префикс, удаляемый reverse proxy; на отдельном origin — пустое значение |
| `TEST_DATABASE_URL` | Только выделенная тестовая БД; тестовый пользователь должен иметь CREATEDB |

Production Compose фиксирует dev mode=false и требует точный proxy peer после
Docker NAT. Дополнительные `POSTGRES_*`/`API_PORT` описаны в
[deployment-инструкции](deploy/README.md).

## Тестирование

`DATABASE_URL` и `TEST_DATABASE_URL` должны указывать на разные выделенные
локальные БД. Integration fixtures создают дополнительные временные БД;
использовать production запрещено.

```bash
python -m pip install -e '.[test]'
alembic upgrade head
DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head
python -m pytest -m "not integration" -q
python -m pytest -m integration -q
python -m pip check
python -m compileall -q app
alembic check
git diff --check
```

Без `TEST_DATABASE_URL` integration-тесты пропускаются; такой запуск не
подтверждает PostgreSQL. [Backend CI](.github/workflows/backend-ci.yml)
использует PostgreSQL 16 и дополнительно запускает
`python tests/docker_auth_smoke.py` — изолированный Nginx TLS → Docker → Uvicorn
smoke. Для него нужен Linux Docker daemon.

## Deployment

[Ручной запуск и конфигурация VPS](deploy/README.md) ·
[GitHub Actions и root-owned скрипт](deploy/AUTODEPLOY.md).
Образ собирается из [Dockerfile](Dockerfile), production-конфигурация —
[deploy/compose.production.yaml](deploy/compose.production.yaml).

PR запускает проверки. Push/workflow_dispatch на `main` после успешного CI
может запустить deploy при `ENABLE_PRODUCTION_DEPLOY=true`. Изменения в PR
сами по себе не подтверждают состояние production. Выпуск, pending migrations,
backup и применимость исторических release notes согласуются оператором отдельно.

## Безопасность

Сервисы приватны по умолчанию. Публичность открывает агрегаты, а журнал/detail
требует analyst/operator с grant либо admin/CLI. Приватные UUID без доступа
дают 404. Browser использует same-origin HttpOnly cookie и Origin/CSRF;
административный ключ в frontend не передаётся. API-ответы имеют `no-store`.

Upstream по умолчанию — HTTPS:443. URL и все DNS-ответы проверяются перед
соединением, сокет закрепляется за проверенным IP, TLS проверяет исходный
hostname. Redirects, environment proxies и retries отключены. Browser cookies,
CSRF и внутренние coverage headers upstream не получает. Лимиты proxy:
request 1 MiB, response 8 MiB, decoded analysis 1 MiB, обмен 30 с, 16 слотов/worker.

Raw query, runtime variables, секретные headers и response bodies не хранятся.
Разрешены masked AST и allowlisted error metadata только в защищённом detail.
Сохранённый SDL — отдельные данные схемы и может содержать author defaults;
публичность снимка следует выбирать с учётом его содержимого.

## Контракты и инструкции

[Указатель всей документации](docs/README.md) ·
[REST API](docs/frontend-api.md) · [Авторизация](docs/frontend-auth.md) ·
[Сервисы](docs/frontend-multi-service.md) ·
[Introspection](docs/frontend-introspection.md) ·
[История и журнал](docs/frontend-history-requests.md) ·
[Статистика, AST и schema metadata](docs/frontend-final-features.md) ·
[GraphQL error details](docs/graphql-error-details.md) ·
[E2E-интеграция](docs/e2e-integration.md).

## Запуск E2E через GitLab

Защищённый запуск пайплайнов, выбор ветки, фоновое отслеживание, настройки
сервера и примеры для фронтенда: [руководство](docs/gitlab-e2e.md).
Для текущего стенда `GITLAB_COVERAGE_URL=https://graphql.rebyte.fun`: QA добавляет
`/graphql` к базовому URL. В других развёртываниях он может включать префикс
reverse proxy.
