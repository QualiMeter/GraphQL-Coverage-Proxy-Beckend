"""Credential boundaries and OpenAPI, without PostgreSQL or invented users."""

import pytest
from fastapi.testclient import TestClient

from app.auth import csrf_token, hash_password, verify_password
from app.main import app
from app.proxy import forwarded_headers
import httpx

ORIGIN = "https://graphql.rebyte.fun"


def test_scrypt_salts_and_password_verification():
    first = hash_password("test-only password 7849")
    second = hash_password("test-only password 7849")
    assert first != second
    assert "test-only" not in first
    assert verify_password("test-only password 7849", first)
    assert not verify_password("wrong password", first)
    assert not verify_password("password", "malformed")
    assert not verify_password("password", "scrypt$999999999$8$1$00$00")


@pytest.mark.parametrize("origin", [None, "null", "https://graphql.rebyte.fun.attacker.test", "http://graphql.rebyte.fun"])
def test_login_rejects_missing_and_cross_origin_before_database(origin):
    headers = {"Origin": origin} if origin else {}
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post("/api/auth/login", json={"username": "operator", "password": "secret"}, headers=headers)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


def test_login_requires_https_and_dev_mode_is_explicit(monkeypatch):
    with TestClient(app, base_url="http://graphql.rebyte.fun") as client:
        assert client.post("/api/auth/login", json={"username": "operator", "password": "secret"},
                           headers={"Origin": ORIGIN}).status_code == 403
    monkeypatch.setenv("COVERAGE_PUBLIC_ORIGIN", "http://localhost:8000")
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "operator", "password": "secret"},
                           headers={"Origin": "http://localhost:8000"}).status_code == 503


@pytest.mark.parametrize("payload", [
    {"username": "operator", "password": "secret" * 100},
    {"username": "operator", "password": "secret", "role": "admin"},
    {"username": "operator", "password": {"token": "never-echo-this"}},
])
def test_validation_never_echoes_credentials(payload):
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post("/api/auth/login", json=payload, headers={"Origin": ORIGIN})
    assert response.status_code == 422
    assert "secret" not in response.text and "never-echo-this" not in response.text
    assert all("input" not in item and "ctx" not in item for item in response.json()["detail"])


async def test_auth_body_limit_applies_to_streamed_bodies():
    async def chunks():
        yield b'{"password":"'
        yield b"sensitive" * 3000
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post("/api/auth/login", content=chunks(), headers={"Origin": ORIGIN})
    assert response.status_code == 413
    assert "sensitive" not in response.text


def test_anonymous_session_and_protected_auth_endpoints(monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", "test-key-" + "x" * 40)
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get("/api/auth/session")
        assert response.json() == {"user": None, "csrfToken": None, "expiresAt": None}
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/auth/users").status_code == 401
        assert client.post("/api/auth/users", json={}).status_code == 401
        # No public bootstrap, registration or admin signup route.
        assert client.post("/api/auth/bootstrap", json={}).status_code == 404
        assert client.post("/api/auth/register", json={}).status_code == 404


def test_internal_session_and_csrf_are_never_forwarded_to_upstream():
    headers = httpx.Headers([
        ("Cookie", "upstream_session=keep; __Host-coverage_session=secret; coverage_dev_session=devsecret"),
        ("X-CSRF-Token", "csrf-secret"), ("X-Coverage-Control", "internal"),
        ("Authorization", "Bearer upstream-only"),
    ])
    forwarded = httpx.Headers(forwarded_headers(headers, strip_forwarding=True))
    assert forwarded["cookie"] == "upstream_session=keep"
    assert forwarded["authorization"] == "Bearer upstream-only"
    assert "x-csrf-token" not in forwarded and "x-coverage-control" not in forwarded
    cookies = httpx.Headers([
        ("Set-Cookie", "__Host-coverage_session=attacker; Secure; Path=/"),
        ("Set-Cookie", "coverage_dev_session=attacker; Path=/"),
        ("Set-Cookie", "upstream_cookie=keep; Path=/"),
    ])
    assert httpx.Headers(forwarded_headers(cookies)).get_list("set-cookie") == ["upstream_cookie=keep; Path=/"]


def test_csrf_token_is_bound_to_browser_session():
    assert csrf_token("a" * 43) != csrf_token("b" * 43)
    assert csrf_token("a" * 43) == csrf_token("a" * 43)
    assert len(csrf_token("a" * 43)) == 64


def test_auth_openapi_contains_real_contract_and_alternative_credentials():
    schema = app.openapi()
    paths = schema["paths"]
    for path in ("/api/auth/login", "/api/auth/session", "/api/auth/logout", "/api/auth/users",
                 "/api/auth/users/{user_id}", "/api/auth/users/{user_id}/services/{service_id}"):
        assert path in paths
    assert schema["components"]["securitySchemes"]["CoverageSession"] == {
        "type": "apiKey", "in": "cookie", "name": "__Host-coverage_session"
    }
    password = schema["components"]["schemas"]["LoginInput"]["properties"]["password"]
    assert password["writeOnly"] is True
    assert "passwordHash" not in schema["components"]["schemas"]["UserOut"]["properties"]
    assert paths["/api/auth/users"]["post"]["security"] == [{"CoverageApiKey": []}, {"CoverageSession": []}]


def test_bootstrap_cli_refuses_noninteractive_password_input():
    import subprocess
    import sys

    response = subprocess.run([sys.executable, "-m", "app.auth_cli", "bootstrap-admin", "admin"],
                              input="never-echo-this-password\n", text=True, capture_output=True)
    assert response.returncode == 1
    assert "interactive terminal" in response.stderr
    assert "never-echo-this-password" not in response.stdout + response.stderr
