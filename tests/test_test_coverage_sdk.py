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


@pytest.mark.parametrize("status,code", [("passed", None), ("failed", "assertion_failed"),
                                        ("failed", "teardown_failed"), ("interrupted", "runner_interrupted")])
def test_finish_retries_only_busy_with_identical_result(monkeypatch, status, code):
    execution = Execution("http://coverage.test", uuid4(), "test-key", "test", "Test")
    execution.started = True
    bodies = []

    def handle(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(409, json={"detail": {"code": "test_requests_in_flight"}})
        return httpx.Response(200, json={"id": execution.id, "runId": execution.run_id, "status": status})

    transport(monkeypatch, handle)
    monkeypatch.setattr(execution, "read", lambda: None)
    monkeypatch.setattr(tracking.time, "sleep", lambda _: None)
    execution.finish(status, 123, code)
    assert execution.finished
    assert bodies == [{"status": status, "durationMs": 123, "failureCode": code}] * 2


def test_finish_busy_is_bounded_and_sessionfinish_preserves_known_result(monkeypatch):
    from coverage_test.pytest_plugin import publish, pytest_sessionfinish
    from types import SimpleNamespace
    execution = Execution("http://coverage.test", uuid4(), "test-key", "test", "Test")
    execution.started = True
    bodies = []
    busy = True

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(409, json={"detail": {"code": "test_requests_in_flight"}}) if busy else httpx.Response(
            200, json={"id": execution.id, "runId": execution.run_id, "status": "failed"})

    transport(monkeypatch, handle)
    monkeypatch.setattr(tracking.time, "sleep", lambda _: None)
    monkeypatch.setattr(execution, "read", lambda: None)
    context = SimpleNamespace(executions={1: execution}, reports=[SimpleNamespace(duration=.123)])
    with pytest.raises(CoverageTrackingError):
        publish(context, "failed", "teardown_failed")
    assert len(bodies) == 3
    busy = False
    session = SimpleNamespace(config=SimpleNamespace(_coverage_test_contexts=[context]), exitstatus=0)
    pytest_sessionfinish(session, 0)
    assert execution.finished
    assert all(body == {"status": "failed", "durationMs": 123, "failureCode": "teardown_failed"} for body in bodies)


def test_finish_does_not_retry_unrelated_conflict(monkeypatch):
    execution = Execution("http://coverage.test", uuid4(), "test-key", "test", "Test")
    execution.started = True
    calls = []
    transport(monkeypatch, lambda request: (calls.append(request) or httpx.Response(409, json={"detail": "different result"})))
    with pytest.raises(CoverageTrackingError, match="HTTP 409"):
        execution.finish("passed", 12)
    assert len(calls) == 1


def test_playwright_concurrency_and_redirect_regressions():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", "--test", "integrations/playwright/index.test.cjs"],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_finish_retry_obeys_elapsed_time_budget(monkeypatch):
    execution = Execution('http://coverage.test', uuid4(), 'test-key', 'test', 'Test')
    execution.started = True
    clock, calls = [0.0], []
    monkeypatch.setattr(tracking.time, 'monotonic', lambda: clock[0])

    def handle(request):
        calls.append(request)
        clock[0] += 6
        return httpx.Response(409, json={'detail': {'code': 'test_requests_in_flight'}})

    transport(monkeypatch, handle)
    with pytest.raises(CoverageTrackingError, match='in flight'):
        execution.finish('passed', 1)
    assert len(calls) == 1
