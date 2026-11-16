"""Real PostgreSQL and HTTPX: concurrent routing, ACL, migrations and row locks."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import proxy
from app.database import get_session, get_session_factory
from app.main import app
from app.models import GraphQLService, ObservedRequest, RequestFieldUsage, SchemaField, SchemaSnapshot, SchemaType, TestRun as Run
from app.services import ConflictError, create_run, create_service, import_sdl, update_service
from app.upstream import PinnedTransport
from test_graphql_proxy import TEST_ADMIN_KEY, raw_response
from test_browser_auth_postgres import auth_context, login, grant  # noqa: F401
from test_postgres_integration import postgres_session  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
async def isolated_database():
    target = os.getenv("TEST_DATABASE_URL")
    if not target:
        pytest.skip("TEST_DATABASE_URL missing; PostgreSQL integration NOT verified")
    database = "coverage_multi_" + uuid.uuid4().hex
    admin = create_async_engine(target, isolation_level="AUTOCOMMIT")
    url = make_url(target).set(database=database).render_as_string(hide_password=False)
    engine = None
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
        result = await asyncio.to_thread(subprocess.run,
            [str(Path(sys.executable).with_name("alembic")), "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[1], env={**os.environ, "DATABASE_URL": url},
            capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        engine = create_async_engine(url)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if engine:
            await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        await admin.dispose()


@pytest.fixture
async def routed_client(isolated_database, monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_ADMIN_KEY)
    monkeypatch.delenv("COVERAGE_LEGACY_PROXY", raising=False)
    monkeypatch.setenv("TARGET_GRAPHQL_URL", "https://wrong.example/graphql")
    async def dependency():
        async with isolated_database() as session:
            yield session
    app.dependency_overrides[get_session_factory] = lambda: isolated_database
    app.dependency_overrides[get_session] = dependency
    app.dependency_overrides[proxy.get_proxy_session] = dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://graphql.rebyte.fun",
                                     headers={"X-Coverage-Api-Key": TEST_ADMIN_KEY}) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session_factory, None)
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(proxy.get_proxy_session, None)


async def api_service_run(client, suffix):
    result = await client.post("/api/services", json={"key": suffix, "name": suffix,
        "description": "Service " + suffix, "endpointUrl": "https://93.184.216.34/" + suffix})
    assert result.status_code == 201, result.text
    service = result.json()
    result = await client.post("/api/schemas/import", json={"serviceId": service["id"], "sdl": f"type Query {{ {suffix}: String }}"})
    assert result.status_code == 201
    snapshot = result.json()
    result = await client.post("/api/runs", json={"snapshotId": snapshot["id"], "name": suffix})
    assert result.status_code == 201
    return service, snapshot, result.json()


async def test_two_parallel_services_route_and_record_only_their_own_snapshot(routed_client, isolated_database, monkeypatch):
    client = routed_client
    a, schema_a, run_a = await api_service_run(client, "alpha")
    b, schema_b, run_b = await api_service_run(client, "beta")
    calls = []
    both_started = asyncio.Event()
    async def upstream(self, request):
        calls.append(request)
        if len(calls) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 3)
        field = request.url.path[1:]
        assert request.content == ('{"query":"{ '+field+' }"}').encode()
        assert "x-coverage-run-id" not in request.headers and "x-coverage-api-key" not in request.headers
        return raw_response(body=('{"data":{"'+field+'":"ok"}}').encode())
    monkeypatch.setattr(PinnedTransport, "handle_async_request", upstream)
    responses = await asyncio.gather(*[
        client.post("/graphql", content=('{"query":"{ '+field+' }"}').encode(), headers={"X-Coverage-Run-Id": run["id"]})
        for field, run in (("alpha", run_a), ("beta", run_b))])
    assert [r.status_code for r in responses] == [200, 200]
    assert {str(r.url) for r in calls} == {a["endpointUrl"], b["endpointUrl"]}
    async with isolated_database() as session:
        for field, snapshot, run in (("alpha", schema_a, run_a), ("beta", schema_b, run_b)):
            rows = (await session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == uuid.UUID(run["id"])))).all()
            assert len(rows) == 1 and rows[0].analysis_status == "success"
            usage = (await session.execute(select(SchemaType.snapshot_id, SchemaField.name)
                .join(SchemaField, SchemaField.type_id == SchemaType.id)
                .join(RequestFieldUsage, RequestFieldUsage.field_id == SchemaField.id)
                .where(RequestFieldUsage.request_id == rows[0].id))).all()
            assert usage == [(uuid.UUID(snapshot["id"]), field)]
    for service, run in ((a, run_a), (b, run_b)):
        assert (await client.post(f"/api/runs/{run['id']}/finish")).status_code == 200
        assert (await client.post("/graphql", json={}, headers={"X-Coverage-Run-Id": run["id"]})).status_code == 409
    assert len(calls) == 2
    assert (await client.post("/graphql", json={})).status_code == 400
    assert (await client.post("/graphql", json={}, headers={"X-Coverage-Run-Id": str(uuid.uuid4())})).status_code == 404
    assert (await client.post("/graphql", json={}, headers={"X-Coverage-Run-Id": "bad"})).status_code == 400
    assert len(calls) == 2


async def test_endpoint_update_checks_all_snapshots_then_preserves_history(routed_client, isolated_database):
    client = routed_client
    service, snapshot, run = await api_service_run(client, "active")
    result = await client.post("/api/schemas/import", json={"serviceId": service["id"], "sdl": "type Query { second: Boolean }"})
    other_snapshot = result.json()
    second_run = (await client.post("/api/runs", json={"snapshotId": other_snapshot["id"], "name": "second"})).json()
    route = f"/api/services/{service['id']}"
    update = {"endpointUrl": "https://93.184.216.35/new"}
    assert (await client.patch(route, json=update)).status_code == 409
    assert (await client.patch(route, json={"name": "Renamed", "description": None, "isPublic": True})).status_code == 200
    assert (await client.post(f"/api/runs/{run['id']}/finish")).status_code == 200
    assert (await client.patch(route, json=update)).status_code == 409
    assert (await client.post(f"/api/runs/{second_run['id']}/finish")).status_code == 200
    changed = await client.patch(route, json=update)
    assert changed.status_code == 200 and changed.json()["endpointUrl"] == update["endpointUrl"]
    assert changed.json()["updatedAt"] >= service["createdAt"]
    async with isolated_database() as session:
        assert await session.get(SchemaSnapshot, uuid.UUID(snapshot["id"])) is not None
        assert await session.get(SchemaSnapshot, uuid.UUID(other_snapshot["id"])) is not None
        assert (await session.get(Run, uuid.UUID(run["id"]))).status == "completed"
    # Public visibility never reveals configuration anonymously.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://graphql.rebyte.fun") as anonymous:
        detail = await anonymous.get(route)
        assert detail.status_code == 200 and detail.json()["endpointUrl"] is None
        assert (await anonymous.get("/api/services")).json()[0]["endpointUrl"] is None
    assert (await client.patch(route, json={"endpointUrl": None})).status_code == 422
    assert (await client.patch(route, json={})).status_code == 422


async def test_operator_configuration_and_run_require_grants(auth_context, postgres_session, monkeypatch):
    client, users, private, public, _, _, private_run, _ = auth_context
    headers, _ = await login(client)
    calls = []
    async def upstream(self, request):
        calls.append(request)
        return raw_response(body=b'{"data":{"privateField":"ok"}}')
    monkeypatch.setattr(PinnedTransport, "handle_async_request", upstream)
    assert (await client.get(f"/api/services/{private.id}")).status_code == 404
    assert (await client.get(f"/api/services/{public.id}")).json()["endpointUrl"] is None
    assert (await client.patch(f"/api/services/{private.id}", json={"name": "deny"}, headers=headers)).status_code == 404
    assert (await client.post("/graphql", json={}, headers={**headers, "X-Coverage-Run-Id": str(private_run.id)})).status_code == 404
    assert calls == []
    await grant(postgres_session, users["operator"], private)
    assert (await client.get(f"/api/services/{private.id}")).json()["endpointUrl"] == private.endpoint_url
    result = await client.get("/api/services")
    assert {item["id"]: item["endpointUrl"] for item in result.json()} == {str(private.id): private.endpoint_url, str(public.id): None}
    assert (await client.patch(f"/api/services/{private.id}", json={"name": "Allowed"}, headers=headers)).status_code == 200
    assert (await client.post("/graphql", json={"query": "{ privateField }"}, headers={**headers, "X-Coverage-Run-Id": str(private_run.id)})).status_code == 200
    assert len(calls) == 1
    client.cookies.clear()
    assert (await client.get(f"/api/services/{private.id}")).status_code == 404


@pytest.mark.parametrize("first", ["run", "update"])
async def test_run_creation_and_endpoint_edit_serialize_on_service_row(isolated_database, first):
    factory = isolated_database
    async with factory() as session:
        service = await create_service(session, "race", "Race", endpoint_url="https://93.184.216.34/graphql")
        snapshot = await import_sdl(session, service.id, "type Query { x: Int }")
    # Deliberately hold a transaction across the competing operation. Each
    # participant uses a separate PostgreSQL connection, not a shared mock.
    async with factory() as owner, factory() as competitor:
        await owner.scalar(select(GraphQLService).where(GraphQLService.id == service.id).with_for_update())
        if first == "run":
            task = asyncio.create_task(update_service(competitor, service.id, {"endpoint_url": "https://93.184.216.35/graphql"}))
        else:
            task = asyncio.create_task(create_run(competitor, snapshot.id, "race", {}))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), .1)
            if first == "run":
                run = await create_run(owner, snapshot.id, "winner", {})
                with pytest.raises(ConflictError):
                    await asyncio.wait_for(task, 3)
                await competitor.rollback()
                assert run.status == "running"
            else:
                await update_service(owner, service.id, {"endpoint_url": "https://93.184.216.35/graphql"})
                assert (await asyncio.wait_for(task, 3)).status == "running"
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with factory() as session:
        row = await session.get(GraphQLService, service.id)
        assert row.endpoint_url == ("https://93.184.216.34/graphql" if first == "run" else "https://93.184.216.35/graphql")


async def test_ssrf_save_and_preconnect_dns_validation_are_both_required(routed_client, isolated_database, monkeypatch, caplog):
    from app import upstream as outbound
    client = routed_client
    for index, url in enumerate(("https://127.0.0.1/graphql", "https://10.0.0.1/graphql",
        "https://169.254.169.254/metadata", "https://[::ffff:127.0.0.1]/graphql",
        "http://93.184.216.34/graphql", "https://93.184.216.34:22/graphql",
        "https://user:private-password@93.184.216.34/graphql", "https://93.184.216.34/graphql?token=private-token")):
        result = await client.post("/api/services", json={"key": f"unsafe{index}", "name": "Unsafe", "endpointUrl": url})
        assert result.status_code == 422, result.text
        assert "private-password" not in result.text and "private-token" not in result.text
    async with isolated_database() as session:
        assert (await session.scalars(select(GraphQLService))).all() == []
    answers = ["93.184.216.34"]
    async def dns(host, port):
        return [(2, 1, 6, "", (address, port)) for address in answers]
    monkeypatch.setattr(outbound, "_resolve", dns)
    result = await client.post("/api/services", json={"key": "dns", "name": "DNS", "endpointUrl": "https://changing.example/graphql"})
    assert result.status_code == 201
    service = result.json()
    snapshot = (await client.post("/api/schemas/import", json={"serviceId": service["id"], "sdl": "type Query { x: Int }"})).json()
    run = (await client.post("/api/runs", json={"snapshotId": snapshot["id"], "name": "DNS"})).json()
    calls = []
    async def network(self, request):
        calls.append(request)
        return raw_response()
    monkeypatch.setattr(PinnedTransport, "handle_async_request", network)
    answers[:] = ["93.184.216.34", "127.0.0.1"]
    result = await client.post("/graphql", json={"query": "mutation { x }"}, headers={"X-Coverage-Run-Id": run["id"]})
    assert result.status_code == 503 and calls == []
    assert "changing.example" not in result.text
    result = await client.patch(f"/api/services/{service['id']}", json={"endpointUrl": "https://other.example/graphql"})
    assert result.status_code == 422
    assert "private-password" not in caplog.text and "private-token" not in caplog.text


async def test_legacy_is_explicit_and_never_overrides_configured_service(routed_client, isolated_database, monkeypatch):
    client = routed_client
    service, _, configured_run = await api_service_run(client, "configured")
    async with isolated_database() as session:
        legacy_service = await create_service(session, "legacy", "Legacy")
        snapshot = await import_sdl(session, legacy_service.id, "type Query { x: Int }")
        legacy_run = await create_run(session, snapshot.id, "Legacy", {})
    calls = []
    async def network(self, request):
        calls.append(request)
        return raw_response(body=b'{"data":{}}')
    monkeypatch.setattr(PinnedTransport, "handle_async_request", network)
    headers = {"X-Coverage-Run-Id": str(legacy_run.id)}
    assert (await client.post("/graphql", json={}, headers=headers)).status_code == 409
    assert (await client.post("/graphql", json={})).status_code == 400
    assert calls == []
    monkeypatch.setenv("COVERAGE_LEGACY_PROXY", "true")
    monkeypatch.setenv("TARGET_GRAPHQL_URL", "https://93.184.216.35/legacy")
    assert (await client.post("/graphql", json={})).status_code == 200
    assert (await client.post("/graphql", json={}, headers=headers)).status_code == 200
    assert (await client.post("/graphql", json={}, headers={"X-Coverage-Run-Id": configured_run["id"]})).status_code == 200
    assert [str(r.url) for r in calls] == ["https://93.184.216.35/legacy"] * 2 + [service["endpointUrl"]]
    for browser_header in ({"Origin": "https://graphql.rebyte.fun"}, {"Sec-Fetch-Mode": "cors"}):
        assert (await client.post("/graphql", json={}, headers=browser_header)).status_code == 400
    assert len(calls) == 3


async def test_two_real_tcp_upstreams_with_parallel_postgres_capture(routed_client, isolated_database, monkeypatch):
    received = {"north": [], "south": []}
    ready = asyncio.Event()
    handlers = []
    async def handle(reader, writer, name):
        try:
            header_block = await reader.readuntil(b"\r\n\r\n")
            length = int(next(line.split(b":", 1)[1] for line in header_block.split(b"\r\n")
                              if line.lower().startswith(b"content-length:")))
            body = await reader.readexactly(length)
            received[name].append(body)
            if all(received.values()):
                ready.set()
            await asyncio.wait_for(ready.wait(), 3)
            payload = ('{"data":{"'+name+'":"ok"}}').encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(payload)).encode() + b"\r\nConnection: close\r\n\r\n" + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    def accept(name):
        def connected(reader, writer):
            task = asyncio.create_task(handle(reader, writer, name))
            handlers.append(task)
        return connected
    servers = [await asyncio.start_server(accept(name), "127.0.0.1", 0) for name in received]
    try:
        origins = [f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}" for server in servers]
        monkeypatch.setenv("COVERAGE_DEV_MODE", "true")
        monkeypatch.setenv("COVERAGE_DEV_UPSTREAM_ORIGINS", ",".join(origins))
        runs = []
        for name, origin in zip(received, origins):
            service = await routed_client.post("/api/services", json={"key": name, "name": name, "endpointUrl": origin + "/graphql"})
            assert service.status_code == 201, service.text
            snapshot = (await routed_client.post("/api/schemas/import", json={"serviceId": service.json()["id"], "sdl": f"type Query {{ {name}: String }}"})).json()
            run = (await routed_client.post("/api/runs", json={"snapshotId": snapshot["id"], "name": name})).json()
            runs.append(run)
        results = await asyncio.gather(*[routed_client.post("/graphql", content=('{"query":"{ '+name+' }"}').encode(),
            headers={"X-Coverage-Run-Id": run["id"]}) for name, run in zip(received, runs)])
        assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
        for name, run in zip(received, runs):
            assert received[name] == [('{"query":"{ '+name+' }"}').encode()]
            async with isolated_database() as session:
                observation = (await session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == uuid.UUID(run["id"])))).one()
                assert observation.analysis_status == "success" and observation.response_has_data is True
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()
        await asyncio.gather(*handlers)
