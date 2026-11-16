"""Service DNS waits own no DB connections; writes recheck live permissions."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import upstream
from app.auth import hash_password, token_hash
from app.auth_models import BrowserSession, ServiceGrant, User
from app.database import get_session, get_session_factory
from app.main import app
from app.models import GraphQLService, TestRun as Run
from app.services import create_run, create_service, import_sdl
from test_browser_auth_postgres import ORIGIN, PASSWORD, TEST_KEY
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration
_encoded_password = None
OLD_ENDPOINT = "https://93.184.216.34/old"
NEW_ENDPOINT = "https://service-dns.example/graphql"


@pytest.fixture
async def service_dns_app(isolated_database, monkeypatch):
    global _encoded_password
    if _encoded_password is None:
        _encoded_password = await asyncio.to_thread(hash_password, PASSWORD)
    # Unmodified SQLAlchemy defaults: pool_size=5, max_overflow=10, timeout=30.
    engine = create_async_engine(isolated_database.kw["bind"].url)
    assert engine.pool.size() == 5 and engine.pool._max_overflow == 10 and engine.pool.timeout() == 30
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
    monkeypatch.setenv("COVERAGE_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.delenv("COVERAGE_DEV_MODE", raising=False)
    async with sessions() as session:
        user = User(username="dns-operator", role="operator", password_hash=_encoded_password)
        session.add(user)
        await session.flush()
        user_id = user.id
        await session.commit()
        service = await create_service(session, "dns-target", "Original", owner_id=user_id, endpoint_url=OLD_ENDPOINT)
        snapshot = await import_sdl(session, service.id, "type Query { ok: Boolean }")
    async def request_session():
        async with sessions() as session:
            yield session
    overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = request_session
    app.dependency_overrides[get_session_factory] = lambda: sessions
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            # Real login, hashed opaque session, Origin and derived CSRF token.
            login = await client.post("/api/auth/login", json={"username": "dns-operator", "password": PASSWORD},
                                      headers={"Origin": ORIGIN})
            assert login.status_code == 200, login.text
            token = client.cookies.get("__Host-coverage_session")
            yield SimpleNamespace(engine=engine, sessions=sessions, client=client, service=service,
                snapshot=snapshot, user_id=user_id, token=token,
                cookie_headers={"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrfToken"]},
                key_headers={"X-Coverage-Api-Key": TEST_KEY})
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        await engine.dispose()


def paused_dns(monkeypatch, count=1):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    addresses = ["93.184.216.35"]
    async def resolve(host, port):
        calls.append((host, port))
        if len(calls) == count:
            started.set()
        await release.wait()
        return [(2, 1, 6, "", (address, port)) for address in addresses]
    # Exercise the real validate_endpoint and its URL / returned-IP SSRF checks.
    monkeypatch.setattr(upstream, "_resolve", resolve)
    return SimpleNamespace(started=started, release=release, calls=calls, addresses=addresses)


async def snapshot_services(context):
    async with context.sessions() as session:
        services = (await session.execute(text("SELECT to_jsonb(t) FROM graphql_services t ORDER BY id"))).scalars().all()
        grants = (await session.execute(text("SELECT to_jsonb(t) FROM auth_service_grants t ORDER BY user_id,service_id"))).scalars().all()
        return services, grants


@pytest.mark.parametrize("method,authentication", [("POST", "cookie"), ("PATCH", "key"), ("PATCH", "cookie")])
async def test_fifteen_service_dns_waits_release_default_pool(service_dns_app, monkeypatch, method, authentication):
    context = service_dns_app
    headers = getattr(context, authentication + "_headers")
    targets = [context.service]
    if method == "PATCH":
        async with context.sessions() as session:
            for index in range(1, 15):
                targets.append(await create_service(session, f"target-{index}", "Original", owner_id=context.user_id,
                                                     endpoint_url=OLD_ENDPOINT))
    dns = paused_dns(monkeypatch, 15)
    async def request(index):
        if method == "POST":
            return await context.client.post("/api/services", headers=headers,
                json={"key": f"created-{index}", "name": f"Created {index}", "endpointUrl": NEW_ENDPOINT})
        return await context.client.patch(f"/api/services/{targets[index].id}", headers=headers,
            json={"name": f"Changed {index}", "description": "Saved", "endpointUrl": NEW_ENDPOINT})
    before = await snapshot_services(context)
    tasks = [asyncio.create_task(request(index)) for index in range(15)]
    try:
        await asyncio.wait_for(dns.started.wait(), 8)
        assert all(not task.done() for task in tasks)
        assert context.engine.pool.checkedout() == 0
        assert await snapshot_services(context) == before
        response = await asyncio.wait_for(context.client.get("/api/services", headers=headers), 2)
        assert response.status_code == 200 and response.json()
        assert context.engine.pool.checkedout() == 0 and not dns.release.is_set()
        dns.release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert [response.status_code for response in responses] == [201 if method == "POST" else 200] * 15
        assert len(dns.calls) == 15
        async with context.sessions() as session:
            if method == "POST":
                rows = (await session.scalars(select(GraphQLService).where(GraphQLService.key.like("created-%")))).all()
                assert len(rows) == 15
                assert {row.key for row in rows} == {f"created-{i}" for i in range(15)}
                assert all(row.endpoint_url == NEW_ENDPOINT for row in rows)
                owner_grants = (await session.scalars(select(ServiceGrant.service_id).where(
                    ServiceGrant.user_id == context.user_id, ServiceGrant.service_id.in_([row.id for row in rows])))).all()
                assert set(owner_grants) == {row.id for row in rows}
            else:
                for index, target in enumerate(targets):
                    row = await session.get(GraphQLService, target.id)
                    assert (row.name, row.description, row.endpoint_url) == (f"Changed {index}", "Saved", NEW_ENDPOINT)
        assert context.engine.pool.checkedout() == 0
    finally:
        dns.release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("method,change,status", [
    ("POST", "session", 401), ("POST", "role", 403), ("POST", "disabled", 401),
    ("PATCH", "session", 401), ("PATCH", "role", 403), ("PATCH", "grant", 404),
    ("PATCH", "grant_and_run", 404), ("PATCH", "run", 409), ("PATCH", "deleted", 404),
])
async def test_service_writes_reauthorize_after_dns(service_dns_app, monkeypatch, method, change, status):
    context = service_dns_app
    dns = paused_dns(monkeypatch)
    before = await snapshot_services(context)
    path = "/api/services" if method == "POST" else f"/api/services/{context.service.id}"
    payload = {"name": "Must not be saved", "endpointUrl": NEW_ENDPOINT}
    if method == "POST":
        payload["key"] = "must-not-exist"
    task = asyncio.create_task(context.client.request(method, path, json=payload, headers=context.cookie_headers))
    try:
        await asyncio.wait_for(dns.started.wait(), 3)
        assert context.engine.pool.checkedout() == 0
        async with context.sessions() as session:
            if change == "session":
                await session.execute(delete(BrowserSession).where(BrowserSession.token_hash == token_hash(context.token)))
            if change == "role":
                await session.execute(update(User).where(User.id == context.user_id).values(role="analyst"))
            if change == "disabled":
                await session.execute(update(User).where(User.id == context.user_id).values(is_active=False))
            if change in ("grant", "grant_and_run"):
                await session.execute(delete(ServiceGrant).where(ServiceGrant.user_id == context.user_id))
            if change == "deleted":
                # Remove the fixture's empty snapshot first (FK is RESTRICT).
                from app.models import SchemaSnapshot, SchemaType, SchemaField
                await session.execute(delete(SchemaField).where(SchemaField.type_id.in_(
                    select(SchemaType.id).where(SchemaType.snapshot_id == context.snapshot.id))))
                await session.execute(delete(SchemaType).where(SchemaType.snapshot_id == context.snapshot.id))
                await session.execute(delete(SchemaSnapshot).where(SchemaSnapshot.id == context.snapshot.id))
                await session.execute(delete(GraphQLService).where(GraphQLService.id == context.service.id))
            await session.commit()
            if change in ("run", "grant_and_run"):
                await create_run(session, context.snapshot.id, "Started during DNS", {})
        expected = await snapshot_services(context)
        dns.release.set()
        response = await asyncio.wait_for(task, 3)
        assert response.status_code == status, response.text
        assert await snapshot_services(context) == expected
        async with context.sessions() as session:
            assert await session.scalar(select(GraphQLService.id).where(GraphQLService.key == "must-not-exist")) is None
            if change != "deleted":
                assert (await session.get(GraphQLService, context.service.id)).endpoint_url == OLD_ENDPOINT
                assert expected[0] == before[0]
            assert await session.scalar(select(func.count()).select_from(Run)) == (1 if change in ("run", "grant_and_run") else 0)
        assert context.engine.pool.checkedout() == 0
    finally:
        dns.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("failure", ["cancel", "unsafe_dns", "commit"])
async def test_failed_service_writes_are_atomic(service_dns_app, monkeypatch, method, failure):
    context = service_dns_app
    before = await snapshot_services(context)
    dns = paused_dns(monkeypatch)
    path = "/api/services" if method == "POST" else f"/api/services/{context.service.id}"
    payload = {"name": "Unsaved", "description": "Unsaved", "isPublic": True, "endpointUrl": NEW_ENDPOINT}
    if method == "POST":
        payload["key"] = "unsaved"
    task = asyncio.create_task(context.client.request(method, path, json=payload, headers=context.cookie_headers))
    try:
        await asyncio.wait_for(dns.started.wait(), 3)
        assert context.engine.pool.checkedout() == 0
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if failure == "unsafe_dns":
                dns.addresses[:] = ["93.184.216.35", "127.0.0.1"]
            else:
                real_commit = AsyncSession.commit
                async def fail_commit(session):
                    # Flush first so rollback must undo both the service and
                    # its owner grant (or all modified PATCH attributes).
                    await session.flush()
                    raise SQLAlchemyError("forced commit failure")
                monkeypatch.setattr(AsyncSession, "commit", fail_commit)
            dns.release.set()
            if failure == "commit":
                with pytest.raises(SQLAlchemyError, match="forced commit failure"):
                    await asyncio.wait_for(task, 3)
                monkeypatch.setattr(AsyncSession, "commit", real_commit)
            else:
                response = await asyncio.wait_for(task, 3)
                assert response.status_code == 422
        assert await snapshot_services(context) == before
        assert context.engine.pool.checkedout() == 0
    finally:
        dns.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_patch_rechecks_permissions_after_service_lock_wait(service_dns_app, monkeypatch):
    context = service_dns_app
    dns = paused_dns(monkeypatch)
    before = await snapshot_services(context)
    blocked = asyncio.Event()
    def select_started(connection, cursor, statement, parameters, sql_context, executemany):
        if "graphql_services" in statement and "FOR UPDATE" in statement:
            blocked.set()
    event.listen(context.engine.sync_engine, "before_cursor_execute", select_started)
    task = asyncio.create_task(context.client.patch(f"/api/services/{context.service.id}",
        json={"endpointUrl": NEW_ENDPOINT, "name": "Unsaved"}, headers=context.cookie_headers))
    try:
        await asyncio.wait_for(dns.started.wait(), 3)
        assert context.engine.pool.checkedout() == 0
        async with context.sessions() as owner:
            await owner.execute(select(GraphQLService).where(GraphQLService.id == context.service.id).with_for_update())
            # Observe the route's own lock attempt, excluding the owner query.
            blocked.clear()
            dns.release.set()
            await asyncio.wait_for(blocked.wait(), 3)
            assert not task.done()
            async with context.sessions() as revoker:
                await revoker.execute(delete(ServiceGrant).where(ServiceGrant.user_id == context.user_id))
                await revoker.commit()
            await owner.commit()
        response = await asyncio.wait_for(task, 3)
        assert response.status_code == 404
        after = await snapshot_services(context)
        assert after[0] == before[0] and after[1] == []
    finally:
        event.remove(context.engine.sync_engine, "before_cursor_execute", select_started)
        dns.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_duplicate_keys_and_patch_without_endpoint(service_dns_app, monkeypatch):
    context = service_dns_app
    dns = paused_dns(monkeypatch, 2)
    tasks = [asyncio.create_task(context.client.post("/api/services", headers=context.cookie_headers,
        json={"key": "same-key", "name": "Same", "endpointUrl": NEW_ENDPOINT})) for _ in range(2)]
    try:
        await asyncio.wait_for(dns.started.wait(), 3)
        assert context.engine.pool.checkedout() == 0
        dns.release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert sorted(response.status_code for response in responses) == [201, 409]
        async with context.sessions() as session:
            row = (await session.scalars(select(GraphQLService).where(GraphQLService.key == "same-key"))).one()
            assert (await session.scalars(select(ServiceGrant).where(ServiceGrant.service_id == row.id))).one().user_id == context.user_id
            await create_run(session, context.snapshot.id, "Active", {})
        async def forbidden_dns(*args):
            pytest.fail("PATCH without endpoint must not resolve DNS")
        monkeypatch.setattr(upstream, "_resolve", forbidden_dns)
        response = await context.client.patch(f"/api/services/{context.service.id}", headers=context.cookie_headers,
                                               json={"name": "Renamed", "description": None, "isPublic": True})
        assert response.status_code == 200
        same_endpoint = await context.client.patch(f"/api/services/{context.service.id}", headers=context.cookie_headers,
                                                    json={"endpointUrl": OLD_ENDPOINT})
        assert same_endpoint.status_code == 200 and same_endpoint.json()["endpointUrl"] == OLD_ENDPOINT
        async with context.sessions() as session:
            row = await session.get(GraphQLService, context.service.id)
            assert (row.name, row.description, row.is_public, row.endpoint_url) == ("Renamed", None, True, OLD_ENDPOINT)
            assert await session.scalar(select(func.count()).select_from(Run).where(Run.status == "running")) == 1
    finally:
        dns.release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
