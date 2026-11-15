import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

import app.api as api
from app.api_models import SchemaOut
from app.main import app
from app.schema_collector import (
    InvalidIntrospectionResponseError, InvalidSchemaTargetURLError, SchemaConnectionError,
    SchemaTimeoutError, IntrospectionDisabledError, SchemaResponseTooLargeError,
)
from app.services import NotFoundError, SchemaEndpointChanged, SchemaEndpointNotConfigured, ConflictError

SERVICE_ID = uuid.uuid4()
ROUTE = "/api/schemas/import/introspection"
TEST_ADMIN_KEY = "test-only-coverage-admin-key-" + "a" * 40


@pytest.fixture(autouse=True)
def configured_admin_key(monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_ADMIN_KEY)


def test_introspection_route_accepts_only_service_id_and_returns_snapshot(monkeypatch):
    invoked = []
    async def fake_import(sessions, service_id, **kwargs):
        invoked.append(service_id)
        return SchemaOut(id=uuid.uuid4(), service_id=service_id, schema_hash="a" * 64,
                         source="introspection", created_at=datetime.now(timezone.utc))
    monkeypatch.setattr(api, "import_introspection_schema", fake_import)
    with TestClient(app, headers={"X-Coverage-Api-Key": TEST_ADMIN_KEY}) as client:
        response = client.post(ROUTE, json={"serviceId": str(SERVICE_ID)})
        assert response.status_code == 201
        assert response.json()["serviceId"] == str(SERVICE_ID)
        assert response.json()["source"] == "introspection"
        assert response.json()["schemaHash"] == "a" * 64
        for extra in ({"targetUrl": "http://user:private-password@attacker.test"},
                      {"headers": {"Authorization": "Bearer private-token"}}, {"credentials": "private-secret"}):
            rejected = client.post(ROUTE, json={"serviceId": str(SERVICE_ID), **extra})
            assert rejected.status_code == 422
            assert all(secret not in rejected.text for secret in ("private-password", "private-token", "private-secret"))
    assert invoked == [SERVICE_ID]


@pytest.mark.parametrize("failure,status,code", [
    (NotFoundError("private-host"), 404, "SERVICE_NOT_FOUND"),
    (SchemaEndpointNotConfigured("private-host"), 409, "SERVICE_ENDPOINT_NOT_CONFIGURED"),
    (SchemaEndpointChanged("private-host"), 409, "SERVICE_ENDPOINT_CHANGED"),
    (ConflictError("SQL private-secret"), 409, "SCHEMA_IMPORT_CONFLICT"),
    (InvalidSchemaTargetURLError("https://user:private-password@private-host"), 409, "UPSTREAM_NOT_ALLOWED"),
    (SchemaConnectionError("private-host:5432"), 502, "UPSTREAM_UNAVAILABLE"),
    (SchemaTimeoutError("private-host:5432"), 504, "UPSTREAM_TIMEOUT"),
    (IntrospectionDisabledError("upstream private-secret"), 409, "INTROSPECTION_DISABLED"),
    (InvalidIntrospectionResponseError("GraphQL error: private-secret"), 502, "INVALID_INTROSPECTION"),
    (SchemaResponseTooLargeError("private-secret"), 502, "INTROSPECTION_LIMIT_EXCEEDED"),
    (SQLAlchemyError("postgresql://user:private-password@private-host"), 503, "SCHEMA_STORAGE_UNAVAILABLE"),
])
def test_introspection_route_sanitizes_failures(monkeypatch, failure, status, code):
    async def fake_import(*args, **kwargs):
        raise failure
    monkeypatch.setattr(api, "import_introspection_schema", fake_import)
    with TestClient(app, headers={"X-Coverage-Api-Key": TEST_ADMIN_KEY}) as client:
        response = client.post(ROUTE, json={"serviceId": str(SERVICE_ID)})
    assert response.status_code == status and response.json()["code"] == code
    assert isinstance(response.json()["detail"], str)
    assert all(secret not in response.text for secret in ("private-password", "private-secret", "private-host"))
    if code == "INTROSPECTION_DISABLED":
        assert "SDL" in response.json()["detail"]


def test_introspection_payload_is_bounded():
    with TestClient(app, headers={"X-Coverage-Api-Key": TEST_ADMIN_KEY}) as client:
        assert client.post(ROUTE, content=b"x" * 16_385).status_code == 413
