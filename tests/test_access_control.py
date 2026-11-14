"""No database is needed to verify authentication and route fail-closed behavior."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app import api
from app.access import API_KEY_HEADER_NAME
from app.main import app


TEST_KEY = "test-only-coverage-admin-key-" + "x" * 40


@pytest.mark.parametrize("path", [
    "/api/services", "/api/schemas/import", "/api/schemas/import/introspection",
    "/api/runs", "/api/runs/" + str(uuid.uuid4()) + "/finish", "/graphql",
])
def test_write_paths_fail_closed_without_config(path, monkeypatch):
    monkeypatch.delenv("COVERAGE_API_KEY", raising=False)
    with TestClient(app) as client:
        response = client.post(path, json={})
    assert response.status_code == 503
    assert response.json() == {"detail": "Coverage API authentication is not configured"}


def test_visibility_and_request_details_require_key(monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
    service_id, run_id = uuid.uuid4(), uuid.uuid4()
    for method, path in [
        ("PATCH", f"/api/services/{service_id}"),
        ("GET", f"/api/runs/{run_id}/requests"),
        ("POST", "/api/services"),
        ("POST", "/graphql"),
    ]:
        with TestClient(app) as client:
            for headers in ({}, {API_KEY_HEADER_NAME: "bad-key"}):
                response = client.request(method, path, json={}, headers=headers)
                assert response.status_code == 401
                assert response.json() == {"detail": "Invalid or missing Coverage API key"}


def test_duplicate_key_header_is_not_accepted(monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
    with TestClient(app) as client:
        response = client.post(
            "/graphql", json={},
            headers=[(API_KEY_HEADER_NAME, TEST_KEY), (API_KEY_HEADER_NAME, TEST_KEY)],
        )
    assert response.status_code == 401


def test_short_key_configuration_is_refused(monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", "guessable-secret")
    with TestClient(app) as client:
        response = client.post(
            "/graphql", json={}, headers={API_KEY_HEADER_NAME: "guessable-secret"}
        )
    assert response.status_code == 503


def test_non_ascii_key_header_is_rejected_not_a_server_error(monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
    with TestClient(app) as client:
        response = client.post(
            "/graphql", json={}, headers=[(b"x-coverage-api-key", b"\xe9" * 40)]
        )
    assert response.status_code == 401


def test_service_list_is_public_by_default_and_admin_sees_private(monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
    calls: list[bool] = []

    async def fake_list_services(session, *, public_only=False):
        calls.append(public_only)
        return []

    monkeypatch.setattr(api, "list_services", fake_list_services)
    with TestClient(app) as client:
        assert client.get("/api/services").status_code == 200
        assert client.get("/api/services", headers={API_KEY_HEADER_NAME: "invalid"}).status_code == 200
        assert client.get("/api/services", headers={API_KEY_HEADER_NAME: TEST_KEY}).status_code == 200
        assert client.get("/health").status_code == 200
        assert client.get("/openapi.json").status_code == 200
    assert calls == [True, True, False]


def test_openapi_exposes_key_requirement_for_proxy_and_writes():
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]
    assert paths["/graphql"]["post"]["security"] == [{"CoverageApiKey": []}]
    assert paths["/api/services"]["post"]["security"] == [{"CoverageApiKey": []}]
    assert "security" not in paths["/api/services"]["get"]

