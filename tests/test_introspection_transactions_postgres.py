"""PostgreSQL pool size 1: no checkout while HTTP waits; reauthorize writes."""
import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import csrf_token, token_hash
from app.auth_models import BrowserSession, ServiceGrant, User
from app.database import get_session, get_session_factory
from app.main import app
from app.models import GraphQLService, SchemaField, SchemaSnapshot, SchemaType
from app.services import create_service
from app.upstream import PinnedTransport
from test_graphql_proxy import TEST_ADMIN_KEY, raw_response
from test_multi_service_postgres import isolated_database  # noqa: F401
from test_service_introspection_postgres import payload

pytestmark = pytest.mark.integration
ROUTE = "/api/schemas/import/introspection"
ORIGIN = "https://graphql.rebyte.fun"


@pytest.fixture
async def single_connection_app(isolated_database, monkeypatch):
    database = isolated_database.kw["bind"].url
    engine = create_async_engine(database, pool_size=1, max_overflow=0, pool_timeout=.4)
    # Expiration also proves the returned DTO doesn't lazy-load after commit.
    sessions = async_sessionmaker(engine, expire_on_commit=True)
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_ADMIN_KEY)
    monkeypatch.setenv("COVERAGE_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.delenv("COVERAGE_DEV_MODE", raising=False)
    monkeypatch.delenv("COVERAGE_LEGACY_PROXY", raising=False)
    async with sessions() as session:
        service = await create_service(session, "pool-target", "Pool target", endpoint_url="https://93.184.216.34/graphql")
        user = User(username="pool-operator", role="operator", password_hash="not-used-in-this-session-test")
        session.add(user)
        await session.flush()
        user_id = user.id
        session.add(ServiceGrant(service_id=service.id, user_id=user_id))
        session.add(BrowserSession(token_hash=token_hash(token), user_id=user_id,
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
            yield SimpleNamespace(engine=engine, sessions=sessions, client=client, service_id=service.id,
                user_id=user_id, token=token, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token(token),
                "Cookie": f"__Host-coverage_session={token}"})
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)
        await engine.dispose()


def slow_upstream(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def network(self, request):
        calls.append(request)
        started.set()
        await release.wait()
        return raw_response(body=json.dumps(payload()).encode())
    monkeypatch.setattr(PinnedTransport, "handle_async_request", network)
    return started, release, calls


@pytest.mark.parametrize("authentication", ["cookie", "api_key"])
async def test_slow_introspection_releases_only_pool_connection(single_connection_app, monkeypatch, authentication):
    context = single_connection_app
    started, release, calls = slow_upstream(monkeypatch)
    headers = context.headers if authentication == "cookie" else {"X-Coverage-Api-Key": TEST_ADMIN_KEY}
    task = asyncio.create_task(context.client.post(ROUTE, json={"serviceId": str(context.service_id)}, headers=headers))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert not task.done() and context.engine.pool.checkedout() == 0
        # This request uses the same size-1, no-overflow PostgreSQL pool. It must
        # finish while the upstream remains blocked, not after release is set.
        response = await asyncio.wait_for(context.client.get("/api/services",
            headers={"X-Coverage-Api-Key": TEST_ADMIN_KEY}), 1)
        assert response.status_code == 200 and response.json()[0]["id"] == str(context.service_id)
        assert not release.is_set() and not task.done()
        assert context.engine.pool.checkedout() == 0
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.status_code == 201, result.text
        assert len(calls) == 1 and context.engine.pool.checkedout() == 0
        # A new import still deduplicates using a separate write transaction.
        again = await context.client.post(ROUTE, json={"serviceId": str(context.service_id)}, headers=headers)
        assert again.status_code == 201 and again.json()["id"] == result.json()["id"]
        assert context.engine.pool.checkedout() == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("change,status", [
    ("grant", 404), ("role", 403), ("disabled_user", 401), ("revoked_session", 401),
    ("expired_session", 401), ("endpoint", 409), ("endpoint_and_grant", 404), ("deleted_service", 404),
])
async def test_rights_and_endpoint_are_fresh_after_http(single_connection_app, monkeypatch, change, status):
    context = single_connection_app
    started, release, calls = slow_upstream(monkeypatch)
    task = asyncio.create_task(context.client.post(ROUTE, json={"serviceId": str(context.service_id)}, headers=context.headers))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert context.engine.pool.checkedout() == 0
        async with context.sessions() as session:
            if change in {"grant", "endpoint_and_grant"}:
                await session.execute(delete(ServiceGrant).where(ServiceGrant.user_id == context.user_id))
            if change == "role":
                await session.execute(update(User).where(User.id == context.user_id).values(role="analyst"))
            if change == "disabled_user":
                await session.execute(update(User).where(User.id == context.user_id).values(is_active=False))
            if change == "revoked_session":
                await session.execute(delete(BrowserSession).where(BrowserSession.user_id == context.user_id))
            if change == "expired_session":
                await session.execute(update(BrowserSession).where(BrowserSession.user_id == context.user_id)
                                      .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            if change in {"endpoint", "endpoint_and_grant"}:
                await session.execute(update(GraphQLService).where(GraphQLService.id == context.service_id)
                                      .values(endpoint_url="https://93.184.216.35/changed"))
            if change == "deleted_service":
                await session.execute(delete(GraphQLService).where(GraphQLService.id == context.service_id))
            await session.commit()
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.status_code == status, result.text
        if change == "endpoint":
            assert result.json()["code"] == "SERVICE_ENDPOINT_CHANGED"
        if status == 404:
            assert result.json() == {"detail": "GraphQL service not found", "code": "SERVICE_NOT_FOUND"}
        async with context.sessions() as session:
            for model in (SchemaSnapshot, SchemaType, SchemaField):
                assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert len(calls) == 1 and context.engine.pool.checkedout() == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_during_upstream_leaves_pool_free(single_connection_app, monkeypatch):
    context = single_connection_app
    started, release, _ = slow_upstream(monkeypatch)
    task = asyncio.create_task(context.client.post(ROUTE, json={"serviceId": str(context.service_id)}, headers=context.headers))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert context.engine.pool.checkedout() == 0
        async with context.sessions() as session:
            assert await session.scalar(text("SELECT 1")) == 1
            assert await session.scalar(select(func.count()).select_from(SchemaSnapshot)) == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
