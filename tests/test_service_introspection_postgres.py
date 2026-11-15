import asyncio
import json
import uuid

import httpx
import pytest
from graphql import build_schema, get_introspection_query, graphql_sync
from sqlalchemy import func, select, text

from app.main import app
from app.models import GraphQLService, ObservedRequest, RequestFieldUsage, SchemaField, SchemaSnapshot, SchemaType, TestRun as Run
from app.services import create_service, import_sdl, import_introspection_schema, create_run, record_observation, SchemaEndpointChanged, update_service
from app.contracts import AnalysisResult, FieldReference, RequestObservationInput
from app.schema_parser import parse_sdl
from app.upstream import PinnedTransport
from test_graphql_proxy import raw_response
from test_multi_service_postgres import isolated_database, routed_client  # noqa: F401
from test_browser_auth_postgres import auth_context, login, grant  # noqa: F401
from test_postgres_integration import postgres_session  # noqa: F401

pytestmark = pytest.mark.integration
ROUTE = "/api/schemas/import/introspection"
SDL_A = "type Query { accounts: [Account!]! } type Account { id: ID! name: String! }"
SDL_B = "type Query { products: [Product!]! } type Product { sku: ID! price: Float }"


def payload(sdl=SDL_A):
    result = graphql_sync(build_schema(sdl), get_introspection_query())
    assert result.errors is None
    return {"data": result.data}


async def register(client, key, endpoint):
    response = await client.post("/api/services", json={"key": key, "name": key, "endpointUrl": endpoint})
    assert response.status_code == 201, response.text
    return response.json()


async def test_two_real_introspection_upstreams_dedup_enrichment_and_history(routed_client, isolated_database, monkeypatch):
    seen = {"accounts": [], "products": []}
    tasks = []
    async def serve(reader, writer, name, sdl):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = int(next(line.split(b":", 1)[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")))
            query = await reader.readexactly(length)
            seen[name].append((head, query))
            document = json.loads(query)["query"]
            if document == get_introspection_query():
                response_payload = payload(sdl)
            else:
                assert name == "products" and document == "{ products { sku } }"
                result = graphql_sync(build_schema(sdl), document, root_value={"products": [{"sku": "p1", "price": 5.0}]})
                assert result.errors is None
                response_payload = {"data": result.data}
            body = json.dumps(response_payload).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    def accept(name, sdl):
        def connected(reader, writer):
            tasks.append(asyncio.create_task(serve(reader, writer, name, sdl)))
        return connected
    servers = [await asyncio.start_server(accept(name, sdl), "127.0.0.1", 0)
               for name, sdl in (("accounts", SDL_A), ("products", SDL_B))]
    try:
        origins = [f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}" for server in servers]
        monkeypatch.setenv("COVERAGE_DEV_MODE", "true")
        monkeypatch.setenv("COVERAGE_DEV_UPSTREAM_ORIGINS", ",".join(origins))
        monkeypatch.setenv("COVERAGE_LEGACY_PROXY", "true")
        monkeypatch.setenv("TARGET_GRAPHQL_URL", "http://127.0.0.1:1/WRONG")
        services = [await register(routed_client, name, origin + "/graphql") for name, origin in zip(seen, origins)]
        async with isolated_database() as session:
            original = await import_sdl(session, uuid.UUID(services[0]["id"]), SDL_A)
            run = await create_run(session, original.id, "Historical", {})
            await record_observation(session, RequestObservationInput(run_id=run.id, http_method="POST",
                response_has_data=True, http_status=200, graphql_has_errors=False, analysis=AnalysisResult(status="success", operation_type="query",
                fields=(FieldReference("Query", "accounts"),))))
            before = [(await session.execute(text(f"SELECT jsonb_agg(to_jsonb(t) ORDER BY t.{order}) FROM {table} t"))).scalar()
                      for table, order in (("schema_types", "id"), ("schema_fields", "id"),
                                           ("test_runs", "id"), ("observed_requests", "id"), ("request_field_usage", "field_id"))]
        results = await asyncio.gather(*[routed_client.post(ROUTE, json={"serviceId": service["id"]}) for service in services])
        assert [r.status_code for r in results] == [201, 201], [r.text for r in results]
        assert results[0].json()["id"] == str(original.id)
        repeated = await routed_client.post(ROUTE, json={"serviceId": services[0]["id"]})
        assert repeated.status_code == 201 and repeated.json()["id"] == str(original.id)
        async with isolated_database() as session:
            snapshots = (await session.scalars(select(SchemaSnapshot))).all()
            assert len(snapshots) == 2
            for service, sdl, response in zip(services, (SDL_A, SDL_B), results):
                row = await session.get(SchemaSnapshot, uuid.UUID(response.json()["id"]))
                assert row.service_id == uuid.UUID(service["id"]) and row.source == "introspection"
                assert row.schema_sdl == parse_sdl(sdl).canonical_sdl and row.schema_hash == parse_sdl(sdl).sha256
                assert row.introspection_json == payload(sdl)
                relations = (await session.execute(select(SchemaType.name, SchemaField.name, SchemaField.target_type_id)
                    .join(SchemaField, SchemaField.type_id == SchemaType.id).where(SchemaType.snapshot_id == row.id))).all()
                assert any(type_name == "Query" and field == ("accounts" if sdl == SDL_A else "products") and target is not None
                           for type_name, field, target in relations)
            after = [(await session.execute(text(f"SELECT jsonb_agg(to_jsonb(t) ORDER BY t.{order}) FROM {table} t {where}"))).scalar()
                for table, order, where in (("schema_types", "id", f"WHERE t.snapshot_id = '{original.id}'"),
                    ("schema_fields", "id", f"WHERE t.type_id IN (SELECT id FROM schema_types WHERE snapshot_id = '{original.id}')"),
                    ("test_runs", "id", ""), ("observed_requests", "id", ""), ("request_field_usage", "field_id", ""))]
            assert after == before
        assert len(seen["accounts"]) == 2 and len(seen["products"]) == 1
        run_b = await routed_client.post("/api/runs", json={"snapshotId": results[1].json()["id"], "name": "Products E2E"})
        assert run_b.status_code == 201
        exchange = await routed_client.post("/graphql", json={"query": "{ products { sku } }"},
            headers={"X-Coverage-Run-Id": run_b.json()["id"]})
        assert exchange.status_code == 200 and exchange.json() == {"data": {"products": [{"sku": "p1"}]}}
        coverage = await routed_client.get(f"/api/runs/{run_b.json()['id']}/coverage")
        assert coverage.status_code == 200
        assert coverage.json()["summary"] == {"totalFields": 3, "coveredFields": 2, "coveragePercent": 66.67}
        assert (await routed_client.post(f"/api/runs/{run_b.json()['id']}/finish")).status_code == 200
        assert len(seen["products"]) == 2
        assert all(b"x-coverage-" not in headers.lower() and b"cookie:" not in headers.lower()
                   for entries in seen.values() for headers, _ in entries)
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        await asyncio.gather(*tasks)


async def test_concurrent_introspection_and_sdl_import_share_identity(routed_client, isolated_database, monkeypatch):
    service = await register(routed_client, "race", "https://93.184.216.34/graphql")
    arrivals = 0
    started = asyncio.Event()
    async def network(self, request):
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            started.set()
        await asyncio.wait_for(started.wait(), 3)
        return raw_response(body=json.dumps(payload()).encode())
    monkeypatch.setattr(PinnedTransport, "handle_async_request", network)
    async def sdl():
        await asyncio.wait_for(started.wait(), 3)
        return await routed_client.post("/api/schemas/import", json={"serviceId": service["id"], "sdl": SDL_A})
    responses = await asyncio.gather(routed_client.post(ROUTE, json={"serviceId": service["id"]}),
                                     routed_client.post(ROUTE, json={"serviceId": service["id"]}), sdl())
    assert [r.status_code for r in responses] == [201] * 3, [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    async with isolated_database() as session:
        row = (await session.scalars(select(SchemaSnapshot))).one()
        assert row.source == "introspection" and row.introspection_json == payload()
        assert await session.scalar(select(func.count()).select_from(SchemaType)) == len(parse_sdl(SDL_A).types)
        assert await session.scalar(select(func.count()).select_from(SchemaField)) == len(parse_sdl(SDL_A).fields)


async def test_endpoint_change_while_fetching_aborts_import(routed_client, isolated_database, monkeypatch):
    service = await register(routed_client, "change", "https://93.184.216.34/graphql")
    async def network(self, request):
        async with isolated_database() as session:
            await update_service(session, uuid.UUID(service["id"]), {"endpoint_url": "https://93.184.216.35/graphql"})
        return raw_response(body=json.dumps(payload()).encode())
    monkeypatch.setattr(PinnedTransport, "handle_async_request", network)
    response = await routed_client.post(ROUTE, json={"serviceId": service["id"]})
    assert response.status_code == 409 and response.json()["code"] == "SERVICE_ENDPOINT_CHANGED"
    async with isolated_database() as session:
        assert await session.scalar(select(func.count()).select_from(SchemaSnapshot)) == 0


async def test_introspection_acl_and_credentials_never_leave_backend(auth_context, postgres_session, monkeypatch, caplog):
    client, users, private, public, *_ = auth_context
    calls = []
    async def network(self, request):
        calls.append(request)
        return raw_response(body=json.dumps(payload()).encode())
    monkeypatch.setattr(PinnedTransport, "handle_async_request", network)
    caplog.set_level("DEBUG")
    assert (await client.post(ROUTE, json={"serviceId": str(private.id)})).status_code == 401
    for role in ("viewer", "analyst", "operator"):
        client.cookies.clear()
        headers, _ = await login(client, role)
        for service in (private, public):
            response = await client.post(ROUTE, json={"serviceId": str(service.id)}, headers=headers)
            assert response.status_code == (404 if role == "operator" else 403)
    assert calls == []
    await grant(postgres_session, users["operator"], private)
    headers, _ = await login(client, "operator")
    client.cookies.set("portal_cookie", "private-cookie")
    headers["Authorization"] = "Bearer private-token"
    response = await client.post(ROUTE, json={"serviceId": str(private.id)}, headers=headers)
    assert response.status_code == 201 and len(calls) == 1
    client.cookies.clear()
    admin_headers, _ = await login(client, "admin")
    assert (await client.post(ROUTE, json={"serviceId": str(public.id)}, headers=admin_headers)).status_code == 201
    assert (await client.post(ROUTE, json={"serviceId": str(uuid.uuid4())}, headers=admin_headers)).json() == {
        "code": "SERVICE_NOT_FOUND", "detail": "GraphQL service not found"}
    assert len(calls) == 2
    assert {str(call.url) for call in calls} == {private.endpoint_url, public.endpoint_url}
    for call in calls:
        assert all(name not in call.headers for name in ("cookie", "x-csrf-token", "x-coverage-api-key", "authorization"))
        assert not any(name.startswith("x-coverage-") for name in call.headers)
    assert all(secret not in caplog.text for secret in ("private-cookie", "private-token"))


async def test_null_endpoint_and_explicit_legacy_fallback(routed_client, isolated_database, monkeypatch):
    async with isolated_database() as session:
        service = await create_service(session, "legacy", "Legacy")
    calls = []
    async def network(self, request):
        calls.append(request)
        return raw_response(body=json.dumps(payload()).encode())
    monkeypatch.setattr(PinnedTransport, "handle_async_request", network)
    response = await routed_client.post(ROUTE, json={"serviceId": str(service.id)})
    assert response.status_code == 409 and response.json()["code"] == "SERVICE_ENDPOINT_NOT_CONFIGURED"
    assert calls == []
    monkeypatch.setenv("COVERAGE_LEGACY_PROXY", "true")
    monkeypatch.setenv("TARGET_GRAPHQL_URL", "https://93.184.216.34/legacy")
    response = await routed_client.post(ROUTE, json={"serviceId": str(service.id)})
    assert response.status_code == 201 and str(calls[0].url) == "https://93.184.216.34/legacy"


async def test_write_failure_rolls_back_all_new_rows_and_existing_enrichment(routed_client, isolated_database, monkeypatch):
    service = await register(routed_client, "atomic", "https://93.184.216.34/graphql")
    async def network(self, request):
        return raw_response(body=json.dumps(payload()).encode())
    monkeypatch.setattr(PinnedTransport, "handle_async_request", network)
    async with isolated_database() as session:
        await session.execute(text("ALTER TABLE schema_fields ADD CONSTRAINT reject_schema_field CHECK (name != 'name')"))
        await session.commit()
    response = await routed_client.post(ROUTE, json={"serviceId": service["id"]})
    assert response.status_code == 503 and response.json()["code"] == "SCHEMA_STORAGE_UNAVAILABLE"
    assert "reject_schema_field" not in response.text and "INSERT" not in response.text
    async with isolated_database() as session:
        for model in (SchemaSnapshot, SchemaType, SchemaField):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert await session.get(GraphQLService, uuid.UUID(service["id"])) is not None
        await session.execute(text("ALTER TABLE schema_fields DROP CONSTRAINT reject_schema_field"))
        await session.commit()
        snapshot = await import_sdl(session, uuid.UUID(service["id"]), SDL_A)
        await session.execute(text("ALTER TABLE schema_snapshots ADD CONSTRAINT reject_enrichment CHECK (source != 'introspection')"))
        await session.commit()
        before = await session.scalar(text("SELECT to_jsonb(s) FROM schema_snapshots s"))
    response = await routed_client.post(ROUTE, json={"serviceId": service["id"]})
    assert response.status_code == 503 and "reject_enrichment" not in response.text
    async with isolated_database() as session:
        assert await session.scalar(text("SELECT to_jsonb(s) FROM schema_snapshots s")) == before
        assert (await session.get(SchemaSnapshot, snapshot.id)).source == "sdl"


async def test_failed_upstream_responses_never_save_partial_schemas(routed_client, isolated_database, monkeypatch):
    service = await register(routed_client, "errors", "https://93.184.216.34/graphql")
    current = [None]
    calls = []
    async def network(self, request):
        calls.append(request)
        result = current[0]
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(PinnedTransport, "handle_async_request", network)
    cases = [
        (raw_response(401, b'{"errors":[{"message":"private-token"}]}'), 502, "UPSTREAM_HTTP_ERROR"),
        (raw_response(503, b"private-host SQL private-password"), 502, "UPSTREAM_HTTP_ERROR"),
        (raw_response(400, b'{"errors":[{"message":"Introspection is disabled: private-token"}]}'), 409, "INTROSPECTION_DISABLED"),
        (raw_response(body=b"not JSON private-token"), 502, "INVALID_JSON"),
        (raw_response(body=b'{"errors":[{"message":"private-token"}]}'), 502, "UPSTREAM_GRAPHQL_ERRORS"),
        (raw_response(body=b'{"data":{}}'), 502, "INVALID_INTROSPECTION"),
        (raw_response(body=b'{"data":{"__schema":{}}}'), 502, "INVALID_INTROSPECTION"),
        (raw_response(307, headers={"Location": "http://169.254.169.254/metadata"}), 502, "UPSTREAM_REDIRECT"),
        (httpx.ConnectError("private-password private-host"), 502, "UPSTREAM_UNAVAILABLE"),
        (httpx.ReadTimeout("private-password private-host"), 504, "UPSTREAM_TIMEOUT"),
    ]
    for result, status, code in cases:
        current[0] = result
        response = await routed_client.post(ROUTE, json={"serviceId": service["id"]})
        assert (response.status_code, response.json()["code"]) == (status, code), response.text
        assert all(secret not in response.text for secret in ("private-token", "private-password", "private-host", "169.254"))
        async with isolated_database() as session:
            for model in (SchemaSnapshot, SchemaType, SchemaField):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert len(calls) == len(cases)
    import app.schema_collector as collector
    monkeypatch.setattr(collector, "MAX_RESPONSE_BYTES", 64)
    current[0] = raw_response(body=b"x" * 65)
    response = await routed_client.post(ROUTE, json={"serviceId": service["id"]})
    assert response.status_code == 502 and response.json()["code"] == "INTROSPECTION_LIMIT_EXCEEDED"
    async with isolated_database() as session:
        assert await session.scalar(select(func.count()).select_from(SchemaSnapshot)) == 0
