"""Identity and HTTP contract checks that do not need a database."""

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import api
from app.main import app
from app.run_analytics import operation_key
from app.services import NotFoundError


def test_operation_key_is_stable_and_unambiguous():
    assert operation_key("a" * 64, None, "query") == operation_key("a" * 64, None, "query")
    identities = [
        ("a" * 64, None, "query"), ("b" * 64, None, "query"),
        ("a" * 64, "null", "query"), ("a" * 64, "", "query"),
        ("a" * 64, None, "mutation"), ("a" * 64, "Users", "query"),
        ("ab", "c", "query"), ("a", "bc", "query"),
    ]
    keys = {operation_key(*item) for item in identities}
    assert len(keys) == len(identities)
    assert all(len(key) == 64 and int(key, 16) >= 0 for key in keys)
    # Freeze the encoding so a refactor cannot silently change frontend identities.
    assert operation_key("abc", None, "query") == "9c896570f12dea4c5f4221e6ef875423db678ba6b163e254b6a5b8c128e757f6"


def test_operation_filters_are_forwarded_and_validated(monkeypatch):
    visible = AsyncMock()
    operations = AsyncMock(return_value=[])
    monkeypatch.setattr(api, "ensure_visible_run", visible)
    monkeypatch.setattr(api, "list_operations", operations)
    run_id = uuid.uuid4()
    with TestClient(app) as client:
        response = client.get(
            f"/api/runs/{run_id}/operations", params={"operationName": "GetUsers", "operationType": "query"}
        )
        assert response.status_code == 200 and response.json() == []
        assert operations.await_args.args[1:] == (run_id, "GetUsers", "query")
        assert visible.await_args.kwargs == {"admin": False}
        assert client.get(f"/api/runs/{run_id}/operations?operationType=invalid").status_code == 422
        assert client.get("/api/runs/not-a-uuid/stats").status_code == 422
    assert operations.await_count == 1


@pytest.mark.parametrize("endpoint,handler", [("operations", "list_operations"), ("stats", "get_run_stats")])
def test_visibility_is_checked_before_aggregating(endpoint, handler, monkeypatch):
    visible = AsyncMock(side_effect=NotFoundError("GraphQL service not found"))
    aggregate = AsyncMock()
    monkeypatch.setattr(api, "ensure_visible_run", visible)
    monkeypatch.setattr(api, handler, aggregate)
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{uuid.uuid4()}/{endpoint}")
    assert response.status_code == 404
    aggregate.assert_not_awaited()


def test_openapi_has_exact_analytics_dtos_and_public_access():
    schema = app.openapi()
    paths = schema["paths"]
    operations = paths["/api/runs/{run_id}/operations"]["get"]
    stats = paths["/api/runs/{run_id}/stats"]["get"]
    assert "security" not in operations and "security" not in stats
    assert "404" in operations["responses"] and "404" in stats["responses"]
    assert {item["name"] for item in operations["parameters"]} == {"run_id", "operationName", "operationType"}
    assert operations["responses"]["200"]["content"]["application/json"]["schema"]["items"] == {
        "$ref": "#/components/schemas/OperationOut"
    }
    models = schema["components"]["schemas"]
    assert set(models["OperationOut"]["properties"]) == {
        "operationKey", "operationName", "operationType", "requestCount", "countedRequestCount",
        "graphqlErrorCount", "httpErrorCount", "avgDurationMs", "lastSeenAt", "fields",
    }
    assert set(models["RunStats"]["properties"]) == {
        "totalRequests", "countedRequests", "uniqueOperations", "queryRequests", "mutationRequests",
        "subscriptionRequests", "httpErrorCount", "graphqlErrorCount", "avgDurationMs",
        "totalFields", "coveredFields", "coveragePercent", "requestTimeline",
    }
    assert set(models["OperationFieldOut"]["properties"]) == {"typeName", "fieldName", "requestCount"}
    assert models["OperationOut"]["properties"]["operationName"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert models["RunStats"]["properties"]["avgDurationMs"]["anyOf"] == [{"type": "number"}, {"type": "null"}]
    assert paths["/graphql"]["post"]["security"] == [{"CoverageApiKey": []}]
    assert paths["/api/runs/{run_id}/requests"]["get"]["security"] == [{"CoverageApiKey": []}]
