from fastapi.testclient import TestClient

from app.main import app
from app.models import (
    GraphQLService, ObservedRequest, RequestFieldUsage, SchemaField,
    SchemaSnapshot, SchemaType, TestRun as RunEntity,
)


def test_database_model_contains_all_seven_tables():
    assert {
        GraphQLService.__tablename__, SchemaSnapshot.__tablename__,
        SchemaType.__tablename__, SchemaField.__tablename__,
        RunEntity.__tablename__, ObservedRequest.__tablename__,
        RequestFieldUsage.__tablename__,
    } == {
        "graphql_services", "schema_snapshots", "schema_types", "schema_fields",
        "test_runs", "observed_requests", "request_field_usage",
    }


def test_frontend_routes_are_documented_by_fastapi():
    with TestClient(app) as client:
        response = client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        assert "/api/services" in paths
        assert "/api/schemas/import" in paths
        assert "/api/schemas/{schema_id}/graph" in paths
        assert "/api/runs/{run_id}/coverage" in paths
        assert "/api/runs/{run_id}/requests" in paths
        assert "post" in paths["/graphql"]


def test_health_is_not_mistaken_for_database_health():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
