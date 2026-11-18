import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.main import app
from app.proxy import coverage_test_id, forwarded_headers
from app.test_executions import ExecutionCreate, ExecutionFinish
import httpx


@pytest.mark.parametrize("values", [["invalid"], [""], [str(uuid.uuid4())] * 2, ["{" + str(uuid.uuid4()) + "}"]])
def test_execution_header_rejects_invalid_identity(values):
    request = Request({"type": "http", "headers": [(b"x-coverage-test-execution-id", value.encode()) for value in values]})
    with pytest.raises(HTTPException) as exc:
        coverage_test_id(request)
    assert exc.value.status_code == 400
    assert "invalid" not in str(exc.value.detail)


def test_execution_header_is_not_forwarded_to_upstream():
    headers = httpx.Headers({"X-Coverage-Test-Execution-Id": str(uuid.uuid4()), "X-Test-Id": "private case",
                            "X-Scenario-Name": "private name", "X-Test-Mode": "partial"})
    result = {key.lower(): value for key, value in forwarded_headers(headers)}
    assert result == {b"x-test-mode": b"partial"}


@pytest.mark.parametrize("changes", [{"testId": "bad\nvalue"}, {"testId": "\ud800"},
                                    {"name": " leading"}, {"attempt": True}, {"framework": "shell"}])
def test_execution_metadata_is_bounded_and_typed(changes):
    payload = {"id": str(uuid.uuid4()), "testId": "tests/test_cart.py::test_cart", "name": "Cart", "framework": "pytest"}
    with pytest.raises(ValidationError):
        ExecutionCreate.model_validate({**payload, **changes})


@pytest.mark.parametrize("payload", [{"status": "passed", "failureCode": "test_failed"},
                                     {"status": "failed", "failureCode": "raw secret"},
                                     {"status": "passed", "durationMs": -1}, {"status": "passed", "durationMs": True},
                                     {"status": "running"}])
def test_final_result_cannot_contain_invalid_state_or_raw_failure(payload):
    with pytest.raises(ValidationError):
        ExecutionFinish.model_validate(payload)


def test_metadata_validation_and_size_limit_do_not_echo_submitted_values(monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", "test-only-key-" + "x" * 40)
    path = f"/api/runs/{uuid.uuid4()}/tests"
    with TestClient(app) as client:
        response = client.post(path, headers={"X-Coverage-Api-Key": "test-only-key-" + "x" * 40},
                               json={"id": str(uuid.uuid4()), "testId": "secret\nvalue", "name": "Example", "framework": "pytest"})
        assert response.status_code == 422
        assert "secret" not in response.text
        assert "input" not in response.text
        response = client.post(path, content=b"x" * 17000)
        assert response.status_code == 413


def test_test_coverage_routes_are_in_openapi():
    paths = app.openapi()["paths"]
    assert "/api/runs/{run_id}/tests" in paths
    assert "/api/runs/{run_id}/tests/{execution_id}/fields" in paths
    headers = paths["/graphql"]["post"]["parameters"]
    assert any(header["name"] == "X-Coverage-Test-Execution-Id" for header in headers)
