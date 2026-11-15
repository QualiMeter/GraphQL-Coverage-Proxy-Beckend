# Два независимых GraphQL API: E2E

Требуются миграция head и два реальных HTTPS GraphQL API на порту 443.
Для локальных HTTP upstream явно настройте API-процесс, например:

```bash
export COVERAGE_DEV_MODE=true
export COVERAGE_DEV_UPSTREAM_ORIGINS=http://127.0.0.1:4010,http://127.0.0.1:4020
```

В Docker origin должен быть достижим **из API-контейнера**, а не браузера.
Для локального cookie login отдельно задаётся COVERAGE_PUBLIC_ORIGIN; не применять
dev-конфигурацию к production. API key хранится только в CI/CLI окружении.

Пример CLI с curl и jq (замените публичные example URL своими реальными API):

```bash
export COVERAGE_BASE_URL=http://localhost:8000
# COVERAGE_API_KEY должен быть заранее безопасно настроен на сервере и в CLI.
service_a=$(curl --fail-with-body -sS "$COVERAGE_BASE_URL/api/services" \
  -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"key":"orders","name":"Orders","endpointUrl":"https://orders.example.com/graphql"}' | jq -r .id)
service_b=$(curl --fail-with-body -sS "$COVERAGE_BASE_URL/api/services" \
  -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" -H 'Content-Type: application/json' \
  -d '{"key":"catalog","name":"Catalog","endpointUrl":"https://catalog.example.com/graphql"}' | jq -r .id)
# Service → Introspection → SchemaSnapshot; каждый сервис использует свой endpoint.
snapshot_a=$(jq -n --arg id "$service_a" '{serviceId:$id}' | \
  curl --fail-with-body -sS "$COVERAGE_BASE_URL/api/schemas/import/introspection" \
  -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" -H 'Content-Type: application/json' --data-binary @- | jq -r .id)
snapshot_b=$(jq -n --arg id "$service_b" '{serviceId:$id}' | \
  curl --fail-with-body -sS "$COVERAGE_BASE_URL/api/schemas/import/introspection" \
  -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" -H 'Content-Type: application/json' --data-binary @- | jq -r .id)
run_a=$(jq -n --arg id "$snapshot_a" '{snapshotId:$id,name:"Orders E2E"}' | \
  curl --fail-with-body -sS "$COVERAGE_BASE_URL/api/runs" -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" \
  -H 'Content-Type: application/json' --data-binary @- | jq -r .id)
run_b=$(jq -n --arg id "$snapshot_b" '{snapshotId:$id,name:"Catalog E2E"}' | \
  curl --fail-with-body -sS "$COVERAGE_BASE_URL/api/runs" -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" \
  -H 'Content-Type: application/json' --data-binary @- | jq -r .id)
curl --fail-with-body -sS "$COVERAGE_BASE_URL/graphql" -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" \
  -H "X-Coverage-Run-Id: $run_a" -H 'Content-Type: application/json' -d '{"query":"{ __typename }"}' &
pid_a=$!
curl --fail-with-body -sS "$COVERAGE_BASE_URL/graphql" -H "X-Coverage-Api-Key: $COVERAGE_API_KEY" \
  -H "X-Coverage-Run-Id: $run_b" -H 'Content-Type: application/json' -d '{"query":"{ __typename }"}' &
pid_b=$!
wait "$pid_a"
wait "$pid_b"
for run in "$run_a" "$run_b"; do
  curl --fail-with-body -sS -X POST "$COVERAGE_BASE_URL/api/runs/$run/finish" \
    -H "X-Coverage-Api-Key: $COVERAGE_API_KEY"
done
```

Если introspection отключена (409 INTROSPECTION_DISABLED) или требует upstream
credentials, импортируйте реальные orders.graphql/catalog.graphql через
POST /api/schemas/import. [Контракт и ошибки](frontend-introspection.md).

В настоящем runner завершайте runs в finally/teardown после ожидания всех запросов.
Один running run на snapshot; несколько snapshots/сервисов работают независимо.
`__typename` — проверка транспорта; для реального покрытия нужны обычные поля SDL.
Не передавайте API key в browser: доверенный тестовый BFF/runner добавляет его на
серверной стороне либо browser использует собственную сессию operator и CSRF.
Отдельные cookie jars нужны каждому upstream, автоматических повторов mutation нет.

Без run ID по умолчанию 400. Временный legacy режим задаётся явно:
COVERAGE_LEGACY_PROXY=true + TARGET_GRAPHQL_URL с безопасным HTTPS default target.
Он допускает CLI без Origin/Sec-Fetch-* без run ID и fallback старых NULL endpoint.
Configured-сервисы всегда используют свой endpoint. Browser всегда требует UUID run.

Локальные проверки:

```bash
pip install -e '.[test]'
# TEST_DATABASE_URL — отдельная временная PostgreSQL БД; пользователь может CREATE DATABASE.
DATABASE_URL="$TEST_DATABASE_URL" alembic upgrade head
python -m pytest -q
python -m pytest tests/test_multi_service_postgres.py tests/test_upstream_security.py tests/test_service_introspection_postgres.py tests/test_introspection_security.py -v
# Только при доступном локальном Docker daemon; создаёт изолированный smoke stack:
python tests/docker_auth_smoke.py
```

В CI PostgreSQL 16, временные migration DB, затем smoke Nginx TLS → Docker NAT →
Uvicorn. Тесты не обращаются к production и не запускают deploy.
