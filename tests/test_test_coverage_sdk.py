import json
from uuid import uuid4

import httpx
import pytest

from coverage_test import client as tracking
from coverage_test import CoverageTrackingError, TestExecution as Execution


def transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(tracking.httpx, "Client", lambda **options: client(transport=httpx.MockTransport(handler), **options))


def test_metadata_retry_preserves_execution_identity(monkeypatch):
    ids = []
    execution = Execution("http://coverage.test", uuid4(), "private-key", "test::name", "Name")

    def handle(request):
        ids.append(json.loads(request.content)["id"])
        if len(ids) == 1:
            raise httpx.ReadTimeout("test-only timeout")
        return httpx.Response(201, json={"id": execution.id, "runId": execution.run_id, "status": "running"})

    transport(monkeypatch, handle)
    execution.start()
    assert ids == [execution.id, execution.id]


def test_metadata_redirect_does_not_forward_credentials(monkeypatch):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://foreign.test/"})

    transport(monkeypatch, handle)
    execution = Execution("http://coverage.test", uuid4(), "private-key", "test", "Test")
    with pytest.raises(CoverageTrackingError, match="HTTP 302"):
        execution.start()
    assert calls == ["http://coverage.test/api/runs/" + execution.run_id + "/tests"]
    assert not execution.started


def test_metadata_does_not_confirm_another_execution(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(201, json={"id": str(uuid4()), "runId": str(uuid4())}))
    execution = Execution("http://coverage.test", uuid4(), "private-key", "test", "Test")
    with pytest.raises(CoverageTrackingError):
        execution.start()
    assert not execution.started


@pytest.mark.parametrize("status,message", [(201, "not JSON"), (409, "HTTP 409")])
def test_metadata_errors_do_not_echo_the_response(monkeypatch, status, message):
    transport(monkeypatch, lambda request: httpx.Response(status, text="private upstream text"))
    execution = Execution("http://coverage.test", uuid4(), "private-key", "test", "Test")
    with pytest.raises(CoverageTrackingError, match=message) as error:
        execution.start()
    assert "private upstream text" not in str(error.value)
    assert not execution.started
