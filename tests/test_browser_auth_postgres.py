"""Browser login → cookie/CSRF → role + service ACL → real PostgreSQL actions."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, text

from app import proxy
from app.auth import bootstrap_admin, hash_password, token_hash
from app.auth_models import BrowserSession, LoginThrottle, ServiceGrant, User
from app.database import get_session
from app.main import app
from app.upstream import PinnedTransport
from app.services import create_run, create_service, import_sdl
from test_postgres_integration import postgres_session  # noqa: F401

pytestmark = pytest.mark.integration
ORIGIN = "https://graphql.rebyte.fun"
PASSWORD = "test-only password 7849"
TEST_KEY = "test-only-admin-key-" + "x" * 40
_encoded_password = None


@pytest.fixture
async def auth_context(postgres_session, monkeypatch):
    global _encoded_password
    if _encoded_password is None:
        _encoded_password = await asyncio.to_thread(hash_password, PASSWORD)
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
    monkeypatch.delenv("COVERAGE_DEV_MODE", raising=False)
    monkeypatch.setenv("COVERAGE_PUBLIC_ORIGIN", ORIGIN)
    users = {}
    for role in ("viewer", "analyst", "operator", "admin"):
        user = User(username=role, role=role, password_hash=_encoded_password)
        postgres_session.add(user)
        users[role] = user
    await postgres_session.commit()
    private = await create_service(postgres_session, "private-" + uuid.uuid4().hex, "Private", endpoint_url="https://93.184.216.34/a")
    public = await create_service(postgres_session, "public-" + uuid.uuid4().hex, "Public", True, endpoint_url="https://93.184.216.35/b")
    private_schema = await import_sdl(postgres_session, private.id, "type Query { privateField: String }")
    public_schema = await import_sdl(postgres_session, public.id, "type Query { publicField: String }")
    private_run = await create_run(postgres_session, private_schema.id, "private", {})
    public_run = await create_run(postgres_session, public_schema.id, "public", {"secret": "not-for-browsers"})

    async def dependency():
        yield postgres_session
    app.dependency_overrides[get_session] = dependency
    app.dependency_overrides[proxy.get_proxy_session] = dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            yield client, users, private, public, private_schema, public_schema, private_run, public_run
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(proxy.get_proxy_session, None)


async def login(client, role="operator"):
    response = await client.post("/api/auth/login", json={"username": role, "password": PASSWORD}, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    data = response.json()
    return {"Origin": ORIGIN, "X-CSRF-Token": data["csrfToken"]}, response


async def grant(session, user, service):
    session.add(ServiceGrant(user_id=user.id, service_id=service.id))
    await session.commit()


async def test_real_login_cookie_hash_rotation_logout_and_no_secrets(auth_context, postgres_session, caplog):
    client, users, *_ = auth_context
    headers, response = await login(client)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-coverage_session=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=43200"):
        assert attribute in cookie
    assert "Domain=" not in cookie
    token = client.cookies.get("__Host-coverage_session")
    row = await postgres_session.get(BrowserSession, token_hash(token))
    assert row is not None and row.user_id == users["operator"].id
    assert token not in row.token_hash
    assert "token" not in response.json() and "password" not in response.text
    current = await client.get("/api/auth/session")
    assert current.json() == response.json()
    assert current.headers["cache-control"] == "no-store"
    assert (await client.post("/api/auth/logout", headers={"Origin": ORIGIN})).status_code == 403
    assert (await client.get("/api/auth/session")).json()["user"] is not None
    new_headers, _ = await login(client)
    assert client.cookies.get("__Host-coverage_session") != token
    assert await postgres_session.get(BrowserSession, token_hash(token), populate_existing=True) is None
    assert (await client.post("/api/auth/logout", headers=headers)).status_code == 403
    assert (await client.post("/api/auth/logout", headers=new_headers)).status_code == 204
    assert (await client.get("/api/auth/session")).json()["user"] is None
    assert await postgres_session.scalar(select(func.count()).select_from(BrowserSession)) == 0
    assert PASSWORD not in caplog.text and token not in caplog.text and new_headers["X-CSRF-Token"] not in caplog.text


@pytest.mark.parametrize("role", ["viewer", "analyst", "operator", "admin"])
async def test_roles_and_nested_private_service_acl(role, auth_context, postgres_session):
    client, users, private, public, private_schema, public_schema, private_run, public_run = auth_context
    headers, _ = await login(client, role)
    private_paths = [f"/api/schemas?serviceId={private.id}", f"/api/schemas/{private_schema.id}/graph",
                     f"/api/runs?snapshotId={private_schema.id}", f"/api/runs/{private_run.id}/coverage",
                     f"/api/runs/{private_run.id}/operations", f"/api/runs/{private_run.id}/stats"]
    for path in private_paths:
        assert (await client.get(path)).status_code == (200 if role == "admin" else 404)
    assert (await client.get(f"/api/runs/{public_run.id}/stats")).status_code == 200
    # Publication authorizes aggregates, never request journal or management.
    journal = f"/api/runs/{public_run.id}/requests"
    assert (await client.get(journal)).status_code == (403 if role == "viewer" else 200 if role == "admin" else 404)
    await grant(postgres_session, users[role], private)
    await grant(postgres_session, users[role], public)
    for path in private_paths:
        assert (await client.get(path)).status_code == 200
    assert (await client.get(journal)).status_code == (403 if role == "viewer" else 200)
    response = await client.patch(f"/api/services/{private.id}", json={"isPublic": True}, headers=headers)
    assert response.status_code == (200 if role in {"operator", "admin"} else 403)
    response = await client.post("/api/schemas/import", json={"serviceId": str(private.id), "sdl": "type Query { x: Int }"}, headers=headers)
    assert response.status_code == (201 if role in {"operator", "admin"} else 403)
    response = await client.post("/api/runs", json={"snapshotId": str(public_schema.id), "name": "conflict"}, headers=headers)
    assert response.status_code == (409 if role in {"operator", "admin"} else 403)
    assert (await client.get("/api/auth/users")).status_code == (200 if role == "admin" else 403)
    response = await client.get(f"/api/runs?snapshotId={public_schema.id}")
    assert response.json()[0]["metadata"] == ({"secret": "not-for-browsers"} if role == "admin" else {})


async def test_csrf_all_write_routes_and_operator_creation_grants(auth_context, postgres_session):
    client, users, private, public, private_schema, public_schema, private_run, public_run = auth_context
    headers, _ = await login(client)
    for method, path, payload in [
        ("POST", "/api/services", {"key": "fresh", "name": "Fresh", "endpointUrl": "https://93.184.216.34/graphql"}),
        ("PATCH", f"/api/services/{private.id}", {"isPublic": True}),
        ("POST", "/api/schemas/import", {"serviceId": str(private.id), "sdl": "type Query { x: Int }"}),
        ("POST", "/api/schemas/import/introspection", {"serviceId": str(private.id)}),
        ("POST", "/api/runs", {"snapshotId": str(public_schema.id), "name": "Fresh", "endpointUrl": "https://93.184.216.34/graphql"}),
        ("POST", f"/api/runs/{public_run.id}/finish", {}),
        ("POST", "/graphql", {"query": "{ x }"}),
    ]:
        for bad_headers in ({}, {"Origin": ORIGIN}, {"Origin": "https://attacker.test", "X-CSRF-Token": headers["X-CSRF-Token"]}):
            assert (await client.request(method, path, json=payload, headers=bad_headers)).status_code == 403
    response = await client.post("/api/services", json={"key": "fresh", "name": "Fresh", "endpointUrl": "https://93.184.216.34/graphql"}, headers=headers)
    assert response.status_code == 201
    service_id = uuid.UUID(response.json()["id"])
    assert await postgres_session.get(ServiceGrant, (users["operator"].id, service_id)) is not None
    # No operator grant: even a public service cannot be modified by guessing its UUID.
    assert (await client.patch(f"/api/services/{public.id}", json={"isPublic": False}, headers=headers)).status_code == 404
    assert (await client.post(f"/api/runs/{public_run.id}/finish", headers=headers)).status_code == 404


async def test_admin_user_management_revokes_sessions_and_protects_last_admin(auth_context, postgres_session):
    client, users, private, *_ = auth_context
    user_ids = {role: user.id for role, user in users.items()}
    headers, _ = await login(client, "admin")
    payload = {"username": "newanalyst", "password": PASSWORD, "role": "analyst"}
    response = await client.post("/api/auth/users", json=payload, headers=headers)
    assert response.status_code == 201 and "password" not in response.text
    user_id = response.json()["id"]
    assert (await client.post("/api/auth/users", json=payload, headers=headers)).status_code == 409
    acl = f"/api/auth/users/{user_id}/services/{private.id}"
    assert (await client.put(acl, headers=headers)).status_code == 204
    assert (await client.put(acl, headers=headers)).status_code == 204
    assert (await client.delete(acl, headers=headers)).status_code == 204
    assert await postgres_session.get(ServiceGrant, (uuid.UUID(user_id), private.id)) is None
    for payload in ({"role": "viewer"}, {"isActive": False}):
        assert (await client.patch(f"/api/auth/users/{user_ids['admin']}", json=payload, headers=headers)).status_code == 409
    operator_headers, _ = await login(client, "operator")
    operator_token = client.cookies.get("__Host-coverage_session")
    assert (await client.patch(f"/api/auth/users/{user_ids['viewer']}", json={"role": "admin"}, headers=operator_headers)).status_code == 403
    # CLI credential can deactivate a user and revoke all previously issued sessions.
    response = await client.patch(f"/api/auth/users/{user_ids['operator']}", json={"isActive": False}, headers={"X-Coverage-Api-Key": TEST_KEY})
    assert response.status_code == 200
    assert await postgres_session.get(BrowserSession, token_hash(operator_token), populate_existing=True) is None
    assert (await client.get("/api/auth/session")).json()["user"] is None
    assert (await client.post("/api/auth/login", json={"username": "operator", "password": PASSWORD}, headers={"Origin": ORIGIN})).status_code == 401


async def test_expired_tampered_duplicate_cookies_are_anonymous(auth_context, postgres_session):
    client, *_ = auth_context
    await login(client)
    token = client.cookies.get("__Host-coverage_session")
    row = await postgres_session.get(BrowserSession, token_hash(token))
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await postgres_session.commit()
    assert (await client.get("/api/auth/session")).json()["user"] is None
    for cookie in ("__Host-coverage_session=invalid", f"__Host-coverage_session={token}; __Host-coverage_session={token}",
                   "__Host-coverage_session=" + "z" * 43):
        assert (await client.get("/api/auth/session", headers={"Cookie": cookie})).json()["user"] is None


async def test_acl_revocation_is_immediate_for_existing_session(auth_context, postgres_session):
    client, users, private, public, schema, _, run, _ = auth_context
    await grant(postgres_session, users["analyst"], private)
    await login(client, "analyst")
    path = f"/api/runs/{run.id}/requests"
    assert (await client.get(path)).status_code == 200
    await client.delete(f"/api/auth/users/{users['analyst'].id}/services/{private.id}", headers={"X-Coverage-Api-Key": TEST_KEY})
    assert (await client.get(path)).status_code == 404
    assert (await client.get(f"/api/schemas/{schema.id}/graph")).status_code == 404


async def test_login_throttle_is_persisted_and_does_not_reveal_users(auth_context, postgres_session):
    client, *_ = auth_context
    # Unknown users receive the same safe error; pre-seed the expensive first attempts.
    from app.auth import throttle_login
    for _ in range(9):
        await throttle_login(postgres_session, "unknownuser", "127.0.0.1")
    bad = {"username": "unknownuser", "password": "sensitive-but-wrong"}
    response = await client.post("/api/auth/login", json=bad, headers={"Origin": ORIGIN})
    assert response.status_code == 401 and response.json() == {"detail": "Invalid username or password"}
    response = await client.post("/api/auth/login", json=bad, headers={"Origin": ORIGIN})
    assert response.status_code == 429 and response.headers["retry-after"] == "900"
    keys = (await postgres_session.scalars(select(LoginThrottle.bucket_key))).all()
    assert all(len(key) == 64 and "unknownuser" not in key for key in keys)


async def test_bootstrap_one_time_and_password_reset_revokes_sessions(postgres_session, monkeypatch):
    user = await bootstrap_admin(postgres_session, "initialadmin", PASSWORD)
    assert user.role == "admin" and user.password_hash != PASSWORD
    with pytest.raises(ValueError, match="already exists"):
        await bootstrap_admin(postgres_session, "secondadmin", PASSWORD)
    assert await postgres_session.scalar(select(func.count()).select_from(User)) == 1


async def test_proxy_browser_acl_csrf_and_cookie_isolation(auth_context, postgres_session, monkeypatch):
    from app.analyzer_adapter import analyze_e2e_query
    from test_graphql_proxy import raw_response

    client, users, private, public, private_schema, public_schema, private_run, public_run = auth_context
    headers, _ = await login(client)
    monkeypatch.setenv("TARGET_GRAPHQL_URL", "https://upstream.example.test/graphql")
    from starlette.datastructures import State
    monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyze_e2e_query}))
    calls = []

    async def upstream(transport, request):
        calls.append(request)
        return raw_response(200, b'{"data":{"privateField":"ok"}}', [
            (b"Set-Cookie", b"__Host-coverage_session=attacker; Secure; Path=/"),
            (b"Set-Cookie", b"upstream_cookie=okay; Secure; Path=/"),
        ])
    monkeypatch.setattr(PinnedTransport, "handle_async_request", upstream)
    run_headers = {**headers, "X-Coverage-Run-Id": str(private_run.id)}
    body = b'{"query":"{ privateField }"}'
    assert (await client.post("/graphql", content=body, headers=run_headers)).status_code == 404
    assert not calls
    await grant(postgres_session, users["operator"], private)
    token = client.cookies.get("__Host-coverage_session")
    response = await client.post("/graphql", content=body, headers=run_headers)
    assert response.status_code == 200 and len(calls) == 1
    assert calls[0].content == body
    assert "x-csrf-token" not in calls[0].headers and "x-coverage-run-id" not in calls[0].headers
    assert token not in str(calls[0].headers)
    assert client.cookies.get("__Host-coverage_session") == token
    assert client.cookies.get("upstream_cookie") is None
    assert "set-cookie" not in response.headers
    assert "cookie" not in calls[0].headers
    assert (await client.post("/graphql", content=body, headers=headers)).status_code == 400
    assert len(calls) == 1


async def test_password_reset_and_role_change_revoke_browser_sessions(auth_context, postgres_session):
    client, users, *_ = auth_context
    for change in ({"password": "different test password 57"}, {"role": "viewer"}):
        if "role" in change:
            # Restore only fixture password so the next login uses the original credential.
            users["operator"].password_hash = _encoded_password
            await postgres_session.commit()
        await login(client)
        token = client.cookies.get("__Host-coverage_session")
        response = await client.patch(f"/api/auth/users/{users['operator'].id}", json=change,
                                      headers={"X-Coverage-Api-Key": TEST_KEY})
        assert response.status_code == 200
        assert await postgres_session.get(BrowserSession, token_hash(token), populate_existing=True) is None
        assert (await client.get("/api/auth/session")).json()["user"] is None


async def test_explicit_dev_cookie_configuration(auth_context, monkeypatch):
    client, *_ = auth_context
    monkeypatch.setenv("COVERAGE_DEV_MODE", "true")
    monkeypatch.setenv("COVERAGE_PUBLIC_ORIGIN", "http://localhost:8000")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as dev:
        response = await dev.post("/api/auth/login", json={"username": "viewer", "password": PASSWORD},
                                  headers={"Origin": "http://localhost:8000"})
        assert response.status_code == 200
        assert response.headers["set-cookie"].startswith("coverage_dev_session=")
        assert "Secure" not in response.headers["set-cookie"]
        assert (await dev.get("/api/auth/session")).json()["user"]["role"] == "viewer"


async def test_browser_cookies_cannot_cross_service_boundary(auth_context, postgres_session, monkeypatch, caplog):
    from app.analyzer_adapter import analyze_e2e_query
    from test_graphql_proxy import raw_response

    caplog.set_level("DEBUG")
    client, users, private, public, _, _, private_run, public_run = auth_context
    headers, _ = await login(client)
    token = client.cookies.get("__Host-coverage_session")
    await grant(postgres_session, users["operator"], private)
    await grant(postgres_session, users["operator"], public)
    from starlette.datastructures import State
    monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyze_e2e_query}))
    calls = []

    async def upstream(transport, request):
        calls.append(request)
        return raw_response(200, b'{"data":{}}', [
            ("Set-Cookie", "portal_session=attacker; Path=/"),
            ("Set-Cookie", "upstream_session=from-service-a; Path=/"),
            ("Set-Cookie", "__Host-coverage_session=attacker; Secure; Path=/"),
        ])
    monkeypatch.setattr(PinnedTransport, "handle_async_request", upstream)
    # Saved service endpoints determine routing; the global target is irrelevant.
    monkeypatch.setenv("TARGET_GRAPHQL_URL", "https://wrong-service.example/graphql")
    for run in (private_run, public_run):
        wire_headers = [*headers.items(), ("X-Coverage-Run-Id", str(run.id)),
                        ("Cookie", f"__Host-coverage_session={token}; portal_session=portal-secret"),
                        ("Cookie", "other_app_session=other-app-secret; upstream_session=service-a-secret")]
        response = await client.post("/graphql", json={"query": "{ __typename }"}, headers=wire_headers)
        assert response.status_code == 200
        assert "set-cookie" not in response.headers
    assert len(calls) == 2
    assert calls[0].url.host == "93.184.216.34" and calls[1].url.host == "93.184.216.35"
    assert all("cookie" not in call.headers for call in calls)
    assert client.cookies.get("__Host-coverage_session") == token
    assert client.cookies.get("portal_session") is None
    assert client.cookies.get("upstream_session") is None
    assert all(value not in caplog.text for value in (token, "portal-secret", "other-app-secret", "service-a-secret"))


async def test_login_csrf_and_logout_through_production_uvicorn_proxy(auth_context, monkeypatch):
    from app.server import server_config

    _, users, *_ = auth_context
    peer = "172.30.8.1"  # Immediate Docker NAT peer, not the Internet client's XFF.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", peer)
    config = server_config()
    config.load()
    headers = {"Origin": ORIGIN, "X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.7"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=config.loaded_app, client=(peer, 32410)),
                                base_url="http://graphql.rebyte.fun") as proxied:
        response = await proxied.post("/api/auth/login", json={"username": "operator", "password": PASSWORD}, headers=headers)
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"] and "HttpOnly" in response.headers["set-cookie"]
        # Transport leg is HTTP; simulate the cookie the external HTTPS browser
        # sends to Nginx, rather than relaxing the client's Secure cookie rules.
        cookie = response.headers["set-cookie"].split(";", 1)[0]
        write_headers = {**headers, "Cookie": cookie, "X-CSRF-Token": response.json()["csrfToken"]}
        result = await proxied.post("/api/services", json={"key": "proxied-auth", "name": "Proxied", "endpointUrl": "https://93.184.216.34/graphql"}, headers=write_headers)
        assert result.status_code == 201
        assert (await proxied.post("/api/auth/logout", headers={**headers, "Cookie": cookie})).status_code == 403
        assert (await proxied.post("/api/auth/logout", headers=write_headers)).status_code == 204
        current = await proxied.get("/api/auth/session", headers={"Cookie": cookie})
        assert current.json()["user"] is None
