"""Independent audit regressions on isolated PostgreSQL and real HTTP routes."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.datastructures import State

from app import proxy
from app.analyzer_adapter import analyze_e2e_query
from app.auth import csrf_token, token_hash
from app.auth_models import BrowserSession, ServiceGrant, User
from app.database import get_session, get_session_factory
from app.main import app
from app.models import ObservedRequest, RequestFieldUsage, TestRun as Run
from app.operation_identity import operation_key
from app.services import create_run, create_service, import_sdl
from app.upstream import PinnedTransport
from test_browser_auth_postgres import ORIGIN, TEST_KEY, auth_context, grant, login  # noqa: F401
from test_graphql_proxy import raw_response
from test_multi_service_postgres import isolated_database  # noqa: F401
from test_postgres_integration import postgres_session  # noqa: F401

pytestmark = pytest.mark.integration
PREVIOUS_REVISION = "20261118_final_features"
CURRENT_REVISION = "20261119_operation_name_text"


@pytest.fixture
async def pool_context(isolated_database, monkeypatch):
    # Match SQLAlchemy's standard 5+10 capacity; only shorten failure timeout.
    engine = create_async_engine(isolated_database.kw["bind"].url,
                                 pool_size=5, max_overflow=10, pool_timeout=.5)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
    monkeypatch.setenv("COVERAGE_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.delenv("COVERAGE_DEV_MODE", raising=False)
    monkeypatch.delenv("COVERAGE_LEGACY_PROXY", raising=False)
    monkeypatch.setattr(proxy, "SessionFactory", sessions)
    monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyze_e2e_query}))
    async with sessions() as session:
        service = await create_service(session, "pool", "Pool", endpoint_url="https://93.184.216.34/graphql")
        snapshot = await import_sdl(session, service.id,
                                    "type Query { ok: Boolean } type Mutation { change: Boolean }")
        run = await create_run(session, snapshot.id, "Pool", {})
        user = User(username="pool-operator", role="operator", password_hash="not-used")
        session.add(user)
        await session.flush()
        session.add(ServiceGrant(service_id=service.id, user_id=user.id))
        session.add(BrowserSession(token_hash=token_hash(token), user_id=user.id,
                                   expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        await session.commit()
    async def request_session():
        async with sessions() as session:
            yield session
    overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = request_session
    app.dependency_overrides[get_session_factory] = lambda: sessions
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            yield SimpleNamespace(engine=engine, sessions=sessions, client=client, run=run, service=service,
                key_headers={"X-Coverage-Api-Key": TEST_KEY},
                cookie_headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token(token),
                                "Cookie": f"__Host-coverage_session={token}"})
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        await engine.dispose()


@pytest.mark.parametrize("authentication", ["key", "cookie"])
@pytest.mark.parametrize("phase", ["dns", "body", "upstream"])
async def test_fifteen_slow_proxy_requests_release_pool(pool_context, monkeypatch, authentication, phase):
    context = pool_context
    headers = getattr(context, authentication + "_headers")
    started, release = asyncio.Event(), asyncio.Event()
    waiting = 0
    calls = []
    source = "query Pool { ok }"
    body = json.dumps({"query": source}).encode()
    async def wait():
        nonlocal waiting
        waiting += 1
        if waiting == 15:
            started.set()
        await release.wait()
    validate = proxy.validate_endpoint
    async def dns(url):
        if phase == "dns":
            await wait()
        return await validate(url)
    async def slow_body():
        await wait()
        yield body
    async def upstream(self, request):
        calls.append(request)
        if phase == "upstream":
            await wait()
        return raw_response()
    monkeypatch.setattr(proxy, "validate_endpoint", dns)
    monkeypatch.setattr(PinnedTransport, "handle_async_request", upstream)
    tasks = [asyncio.create_task(context.client.post("/graphql", content=slow_body() if phase == "body" else body,
        headers={**headers, "X-Coverage-Run-Id": str(context.run.id)})) for _ in range(15)]
    try:
        await asyncio.wait_for(started.wait(), 8)
        assert all(not task.done() for task in tasks)
        assert context.engine.pool.checkedout() == 0
        response = await asyncio.wait_for(context.client.get("/api/services", headers=headers), 2)
        assert response.status_code == 200 and response.json()[0]["id"] == str(context.service.id)
        assert not release.is_set() and context.engine.pool.checkedout() == 0
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert [response.status_code for response in responses] == [200] * 15
        assert len(calls) == 15
        assert all(request.content == body for request in calls)
        async with context.sessions() as session:
            rows = (await session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == context.run.id))).all()
            assert len(rows) == 15
            expected_hash = hashlib.sha256(source.encode()).hexdigest()
            assert all(row.analysis_status == "success" and row.query_hash == expected_hash
                       and row.operation_key == operation_key(expected_hash, "Pool", "query") for row in rows)
            assert await session.scalar(select(func.count()).select_from(RequestFieldUsage)) == 15
        assert context.engine.pool.checkedout() == 0
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("authentication", ["key", "cookie"])
async def test_finish_while_mutation_waits_preserves_lock_contract(pool_context, monkeypatch, authentication):
    context = pool_context
    headers = getattr(context, authentication + "_headers")
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def upstream(self, request):
        calls.append(request)
        started.set()
        await release.wait()
        return raw_response(body=b'{"data":{"change":true}}')
    monkeypatch.setattr(PinnedTransport, "handle_async_request", upstream)
    task = asyncio.create_task(context.client.post("/graphql", json={"query": "mutation Save { change }"},
        headers={**headers, "X-Coverage-Run-Id": str(context.run.id)}))
    try:
        await asyncio.wait_for(started.wait(), 3)
        finished = await asyncio.wait_for(context.client.post(f"/api/runs/{context.run.id}/finish", headers=headers), 2)
        assert finished.status_code == 200
        release.set()
        response = await asyncio.wait_for(task, 3)
        assert response.status_code == 502
        assert "upstream may have executed" in response.text and "Do not retry automatically" in response.text
        assert len(calls) == 1
        async with context.sessions() as session:
            assert (await session.get(Run, context.run.id)).status == "completed"
            assert await session.scalar(select(func.count()).select_from(ObservedRequest)) == 0
            assert await session.scalar(select(func.count()).select_from(RequestFieldUsage)) == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_mutation_commit_failure_never_repeats_upstream(pool_context, monkeypatch):
    context = pool_context
    calls = []
    async def upstream(self, request):
        calls.append(request)
        return raw_response(body=b'{"data":{"change":true}}')
    async def fail_commit(self):
        raise SQLAlchemyError("private-storage-error")
    monkeypatch.setattr(PinnedTransport, "handle_async_request", upstream)
    monkeypatch.setattr(AsyncSession, "commit", fail_commit)
    response = await context.client.post("/graphql", json={"query": "mutation Save { change }"},
        headers={**context.key_headers, "X-Coverage-Run-Id": str(context.run.id)})
    assert response.status_code == 502 and len(calls) == 1
    assert "upstream may have executed" in response.text and "Do not retry automatically" in response.text
    assert "private-storage-error" not in response.text
    async with context.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ObservedRequest)) == 0
        assert await session.scalar(select(func.count()).select_from(RequestFieldUsage)) == 0


@pytest.mark.parametrize("role", ["viewer", "analyst", "operator", "admin", "cli"])
async def test_run_metadata_policy_for_all_responses(auth_context, postgres_session, role):
    client, users, private, _, snapshot, _, run, _ = auth_context
    marker = {"private": "stage6-admin-only-marker"}
    # Admin creates the marked run through the real API, then another identity reads/finishes it.
    admin_headers, _ = await login(client, "admin")
    assert (await client.post(f"/api/runs/{run.id}/finish", headers=admin_headers)).status_code == 200
    created = await client.post("/api/runs", json={"snapshotId": str(snapshot.id), "name": "Marked", "metadata": marker}, headers=admin_headers)
    assert created.status_code == 201 and created.json()["metadata"] == marker
    run_id = created.json()["id"]
    if role == "cli":
        client.cookies.clear()
        headers = {"X-Coverage-Api-Key": TEST_KEY}
    else:
        headers, _ = await login(client, role)
    if role not in ("admin", "cli"):
        assert (await client.get("/api/runs", params={"snapshotId": str(snapshot.id)})).status_code == 404
        denied = await client.post(f"/api/runs/{run_id}/finish", headers=headers)
        assert denied.status_code == (404 if role == "operator" else 403)
        await grant(postgres_session, users[role], private)
    expected = marker if role in ("admin", "cli") else {}
    history = await client.get("/api/runs", params={"snapshotId": str(snapshot.id)}, headers=headers)
    assert history.status_code == 200
    assert next(row for row in history.json() if row["id"] == run_id)["metadata"] == expected
    if role != "cli":
        for bad in ({}, {"Origin": ORIGIN}, {**headers, "Origin": "https://attacker.test"}):
            assert (await client.post(f"/api/runs/{run_id}/finish", headers=bad)).status_code == 403
    finished = await client.post(f"/api/runs/{run_id}/finish", headers=headers)
    assert finished.status_code == (403 if role in ("viewer", "analyst") else 200)
    if finished.status_code == 200:
        assert finished.json()["metadata"] == expected
        created = await client.post("/api/runs", json={"snapshotId": str(snapshot.id), "name": "Next", "metadata": marker}, headers=headers)
        assert created.status_code == 201 and created.json()["metadata"] == expected
    else:
        denied = await client.post("/api/runs", json={"snapshotId": str(snapshot.id), "name": "Denied", "metadata": marker}, headers=headers)
        assert denied.status_code == 403
    stored = await postgres_session.get(Run, run_id, populate_existing=True)
    assert stored.metadata_json == marker
    admin_headers, _ = await login(client, "admin")
    history = await client.get("/api/runs", params={"snapshotId": str(snapshot.id)}, headers=admin_headers)
    assert next(row for row in history.json() if row["id"] == run_id)["metadata"] == marker


@pytest.mark.parametrize("length", [255, 256, 1024])
@pytest.mark.parametrize("kind", ["query", "mutation"])
async def test_full_operation_names_proxy_journal_and_grouping(pool_context, monkeypatch, length, kind):
    context = pool_context
    client, run = context.client, context.run
    calls = []
    async def upstream(self, request):
        calls.append(request)
        return raw_response(body=b'{"data":{"ok":true,"change":true}}')
    monkeypatch.setattr(PinnedTransport, "handle_async_request", upstream)
    name = "N" * length
    for operation_name in (name, name + "X"):
        selection = "ok" if kind == "query" else "change"
        source = f"{kind} {operation_name} {{ {selection} }}"
        response = await client.post("/graphql", json={"query": source, "operationName": operation_name},
                                     headers={**context.key_headers, "X-Coverage-Run-Id": str(run.id)})
        assert response.status_code == 200, response.text
        expected_hash = hashlib.sha256(source.encode()).hexdigest()
        async with context.sessions() as session:
            row = await session.scalar(select(ObservedRequest).where(ObservedRequest.run_id == run.id,
                                                                    ObservedRequest.operation_name == operation_name))
            assert row.operation_name == operation_name and row.analysis_status == "success"
            assert row.query_hash == expected_hash
            assert row.operation_key == operation_key(expected_hash, operation_name, kind)
    assert len(calls) == 2  # one call per operation, including mutation
    assert all(json.loads(call.content)["operationName"] in (name, name + "X") for call in calls)
    for operation_name in (name, name + "X"):
        journal = await client.get(f"/api/runs/{run.id}/requests", params={"operationName": operation_name}, headers=context.key_headers)
        assert journal.status_code == 200 and journal.headers["X-Total-Count"] == "1"
        assert [row["operationName"] for row in journal.json()] == [operation_name]
        operations = await client.get(f"/api/runs/{run.id}/operations", params={"operationName": operation_name}, headers=context.key_headers)
        assert operations.status_code == 200
        assert [row["operationName"] for row in operations.json()] == [operation_name]
        assert operations.json()[0]["operationKey"] == journal.json()[0]["operationKey"]
    assert (await client.get(f"/api/runs/{run.id}/requests", params={"operationName": name.lower()}, headers=context.key_headers)).json() == []
    assert len((await client.get(f"/api/runs/{run.id}/operations", headers=context.key_headers)).json()) == 2


async def test_operation_name_migration_preserves_rows_and_refuses_lossy_downgrade(isolated_database):
    url = isolated_database.kw["bind"].url.render_as_string(hide_password=False)
    async def migrate(*args, success=True):
        result = await asyncio.to_thread(subprocess.run, [str(Path(sys.executable).with_name("alembic")), *args],
            cwd=Path(__file__).resolve().parents[1], env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True)
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return result
    async def rows():
        async with isolated_database() as session:
            return (await session.execute(text("SELECT to_jsonb(t) FROM observed_requests t ORDER BY id"))).scalars().all()
    async def column_type():
        async with isolated_database() as session:
            return await session.scalar(text("SELECT data_type FROM information_schema.columns WHERE table_name='observed_requests' AND column_name='operation_name'"))
    async with isolated_database() as session:
        service = await create_service(session, "migration", "Migration")
        snapshot = await import_sdl(session, service.id, "type Query { ok: Boolean }")
        run = await create_run(session, snapshot.id, "Migration", {})
    await migrate("downgrade", PREVIOUS_REVISION)
    assert await column_type() == "character varying"
    async with isolated_database() as session:
        for name in (None, "Legacy", "N" * 255):
            # Insert the historical column set, independent of current ORM columns.
            await session.execute(text("""INSERT INTO observed_requests
                (id,run_id,operation_name,operation_type,query_hash,http_method,analysis_status,duration_ms)
                VALUES (gen_random_uuid(),:run,:name,'query',:hash,'POST','success',42)"""),
                {"run": run.id, "name": name, "hash": "a" * 64})
        await session.commit()
    before = await rows()
    await migrate("upgrade", CURRENT_REVISION)
    assert await column_type() == "text" and await rows() == before
    # Safe downgrade remains supported, including null and boundary names.
    await migrate("downgrade", PREVIOUS_REVISION)
    assert await rows() == before
    await migrate("upgrade", CURRENT_REVISION)
    async with isolated_database() as session:
        await session.execute(text("""INSERT INTO observed_requests
            (id,run_id,operation_name,operation_type,query_hash,http_method,analysis_status)
            VALUES (gen_random_uuid(),:run,:name,'mutation',:hash,'POST','success')"""),
            {"run": run.id, "name": "N" * 256, "hash": "b" * 64})
        await session.commit()
    with_long_name = await rows()
    failed = await migrate("downgrade", PREVIOUS_REVISION, success=False)
    assert "operation names longer than 255 characters exist" in failed.stderr
    assert await column_type() == "text" and await rows() == with_long_name
    async with isolated_database() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT_REVISION
    await migrate("upgrade", "head")
    await migrate("check")
