import uuid

import pytest
from pydantic import ValidationError

from app.main import app
from app.test_executions import ExecutionCreate


@pytest.mark.parametrize("key", ["", " ", " team", "team\nsecret", "x" * 129, "\ud800"])
def test_group_keys_reject_invalid_values(key):
    with pytest.raises(ValidationError):
        ExecutionCreate(id=uuid.uuid4(), testId="test", name="Test", framework="pytest", suiteKey=key)


def test_group_routes_and_filters_are_documented():
    paths = app.openapi()["paths"]
    route = "/api/runs/{run_id}/test-coverage/groups"
    assert route in paths
    parameters = {p["name"] for p in paths[route]["get"]["parameters"]}
    assert {"groupBy", "teamKey", "suiteKey", "unassignedTeam", "unassignedSuite"} <= parameters
