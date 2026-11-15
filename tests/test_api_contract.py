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
        assert "/api/schemas/import/introspection" in paths
        assert "/api/schemas/{schema_id}/graph" in paths
        assert "/api/runs/{run_id}/coverage" in paths
        assert "/api/runs/{run_id}/requests" in paths
        assert "post" in paths["/graphql"]


def test_health_is_not_mistaken_for_database_health():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_service_validation_never_echoes_submitted_secrets(monkeypatch):
    from test_graphql_proxy import TEST_ADMIN_KEY
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_ADMIN_KEY)
    with TestClient(app, headers={"X-Coverage-Api-Key": TEST_ADMIN_KEY}) as client:
        response = client.post("/api/services", json={"key": "Secret supplied in invalid key", "name": "Test",
            "endpointUrl": "https://user:private-password@example.com/graphql?token=private-token"})
        assert response.status_code == 422
        assert all(secret not in response.text for secret in ("private-password", "private-token", "Secret supplied"))
        response = client.post("/api/services", content=b"x" * 16_385)
        assert response.status_code == 413


def test_history_journal_openapi_typed_filters_and_detail():
    schema = app.openapi()
    paths = schema['paths']
    history = paths['/api/runs']['get']
    journal = paths['/api/runs/{run_id}/requests']['get']
    for operation, required in ((history, {'snapshotId','serviceId','status','from','to','limit','offset'}),
        (journal, {'operationKey','operationName','operationType','typeName','fieldName','analysisStatus',
                   'graphqlHasErrors','httpStatus','httpStatusClass','from','to','sortBy','sortOrder','limit','offset'})):
        assert required <= {p['name'] for p in operation['parameters']}
        assert operation['responses']['200']['headers']['X-Total-Count']['schema']['type'] == 'integer'
        assert operation['responses']['200']['content']['application/json']['schema']['type'] == 'array'
    params = {p['name']: p['schema'] for p in journal['parameters']}
    assert params['sortBy']['enum'] == ['createdAt', 'durationMs']
    assert params['sortOrder']['enum'] == ['asc', 'desc']
    assert params['limit']['minimum'] == 1 and params['limit']['maximum'] == 500
    assert params['offset']['minimum'] == 0
    detail = paths['/api/runs/{run_id}/requests/{request_id}']['get']
    assert detail['responses']['200']['content']['application/json']['schema']['$ref'].endswith('/RequestDetailOut')
    assert 'operationKey' in schema['components']['schemas']['RequestOut']['properties']
    assert {'countedInCoverage','selectedFields'} <= schema['components']['schemas']['RequestDetailOut']['properties'].keys()
