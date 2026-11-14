"""HTTP contract for importing a configured GraphQL endpoint via introspection."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.api as api
from app.api_models import SchemaOut
from app.main import app
from app.schema_collector import (
    InvalidIntrospectionResponseError,
    InvalidSchemaTargetURLError,
    SchemaConnectionError,
)
from app.services import NotFoundError


SERVICE_ID = uuid.uuid4()
TARGET_URL = "https://graphql.example.test/graphql"
ROUTE = "/api/schemas/import/introspection"


def test_introspection_route_uses_config_only_and_returns_snapshot(monkeypatch):
    invoked: list[tuple] = []
    monkeypatch.setattr(api, "target_graphql_url", lambda: TARGET_URL)

    async def fake_import(session, service_id, target_url):
        invoked.append((service_id, target_url))
        return SchemaOut(
            id=uuid.uuid4(),
            service_id=service_id,
            schema_hash="a" * 64,
            source="introspection",
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(api, "import_introspection_schema", fake_import)

    with TestClient(app) as client:
        response = client.post(ROUTE, json={"serviceId": str(SERVICE_ID)})
        injected_url = client.post(
            ROUTE,
            json={"serviceId": str(SERVICE_ID), "targetUrl": "http://attacker.test/graphql"},
        )

    assert response.status_code == 201
    data = response.json()
    assert data["serviceId"] == str(SERVICE_ID)
    assert data["source"] == "introspection"
    assert data["schemaHash"] == "a" * 64
    assert invoked == [(SERVICE_ID, TARGET_URL)]
    assert injected_url.status_code == 422


def test_introspection_route_requires_configured_target(monkeypatch):
    monkeypatch.setattr(api, "target_graphql_url", lambda: "")

    async def should_not_run(*_args, **_kwargs):
        pytest.fail("Collector must not run without a configured endpoint")

    monkeypatch.setattr(api, "import_introspection_schema", should_not_run)
    with TestClient(app) as client:
        response = client.post(ROUTE, json={"serviceId": str(SERVICE_ID)})

    assert response.status_code == 503
    assert response.json()["detail"] == "TARGET_GRAPHQL_URL is not configured"


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_detail"),
    [
        (NotFoundError("GraphQL service not found"), 404, "GraphQL service not found"),
        (
            InvalidSchemaTargetURLError("https://user:password@example.test"),
            503,
            "TARGET_GRAPHQL_URL is not configured correctly",
        ),
        (
            SchemaConnectionError("https://user:password@example.test"),
            502,
            "GraphQL schema introspection failed",
        ),
        (
            InvalidIntrospectionResponseError("GraphQL error: secret data"),
            502,
            "GraphQL schema introspection failed",
        ),
    ],
)
def test_introspection_route_sanitizes_failures(
    monkeypatch, failure, expected_status, expected_detail
):
    monkeypatch.setattr(api, "target_graphql_url", lambda: TARGET_URL)

    async def fake_import(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(api, "import_introspection_schema", fake_import)
    with TestClient(app) as client:
        response = client.post(ROUTE, json={"serviceId": str(SERVICE_ID)})

    assert response.status_code == expected_status
    assert response.json()["detail"] == expected_detail
    assert "password" not in response.text
    assert "secret data" not in response.text
