import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import httpx
import pytest
from starlette.datastructures import State
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.analyzer_adapter import analyze_e2e_query
from app.main import app
from app.models import ObservedRequest, TestExecution as Execution
from app.services import create_run, create_service, import_sdl
from test_graphql_proxy import raw_response, upstream
from test_multi_service_postgres import isolated_database, routed_client
from test_browser_auth_postgres import auth_context, grant, login
from test_postgres_integration import postgres_session

pytestmark = pytest.mark.integration
SDL = "type Query { user: User } type User { id: ID! name: String email: String }"
QUERY = "{ user { id name } }"
BODY = b'{"data":{"user":{"id":"1","name":"Anna"}}}'


@pytest.fixture
async def context(isolated_database, routed_client, monkeypatch, upstream):
    monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyze_e2e_query}))
    async with isolated_database() as session:
        service = await create_service(session, "tc-" + uuid.uuid4().hex, "Test Coverage", endpoint_url="https://93.184.216.34/graphql")
        snapshot = await import_sdl(session, service.id, SDL)
        run = await create_run(session, snapshot.id, "Tracked tests", {})
    requests = upstream(lambda request: raw_response(body=BODY))
    return routed_client, isolated_database, str(run.id), requests


async def start(client, run_id, *, test_id="tests/cart.py::test_cart", attempt=1, ident=None, name="Cart"):
    payload = {"id": str(ident or uuid.uuid4()), "testId": test_id, "name": name, "framework": "pytest", "attempt": attempt}
    response = await client.post(f"/api/runs/{run_id}/tests", json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload


async def exchange(client, run_id, execution_id=None, query=QUERY):
    headers = {"X-Coverage-Run-Id": run_id}
    if execution_id is not None:
        headers["X-Coverage-Test-Execution-Id"] = execution_id
    return await client.post("/graphql", headers=headers, json={"query": query})


async def finish(client, run_id, execution_id, status="passed", **kwargs):
    return await client.post(f"/api/runs/{run_id}/tests/{execution_id}/finish", json={"status": status, "durationMs": 12, **kwargs})


async def test_two_tests_share_fields_without_losing_their_distinct_results(context):
    client, sessions, run_id, requests = context
    first, _ = await start(client, run_id, test_id="cart::passed")
    second, _ = await start(client, run_id, test_id="cart::failed")
    assert (await exchange(client, run_id, first["id"])).status_code == 200
    assert (await exchange(client, run_id, second["id"], "{ user { id } }")).status_code == 200
    assert (await finish(client, run_id, first["id"])).status_code == 200
    failed = await finish(client, run_id, second["id"], "failed", failureCode="assertion_failed")
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed" and failed.json()["coveredFieldCount"] == 2
    fields = (await client.get(f"/api/runs/{run_id}/tests/{first['id']}/fields")).json()
    assert {(f["typeName"], f["fieldName"], f["hits"], f["observedCount"]) for f in fields} == {
        ("Query", "user", 1, 1), ("User", "id", 1, 1), ("User", "name", 1, 1)}
    reverse = await client.get(f"/api/runs/{run_id}/tests", params={"typeName": "User", "fieldName": "id"})
    assert reverse.headers["X-Total-Count"] == "2"
    assert {row["status"] for row in reverse.json()} == {"passed", "failed"}
    rows = await client.get(f"/api/runs/{run_id}/requests", params={"testExecutionId": second["id"]})
    assert len(rows.json()) == 1 and rows.json()[0]["testExecutionId"] == second["id"]
    detail = (await client.get(f"/api/runs/{run_id}/requests/{rows.json()[0]['id']}")).json()
    assert detail["testExecutionId"] == second["id"] and detail["countedInCoverage"]
    graph = (await client.get(f"/api/runs/{run_id}/coverage")).json()
    assert graph["summary"] == {"coveredFields": 3, "totalFields": 4, "coveragePercent": 75.0}
    hits = {t["name"] + "." + f["name"]: f["hits"] for t in graph["types"] for f in t["fields"]}
    assert hits == {"Query.user": 2, "User.id": 2, "User.name": 1, "User.email": 0}
    summary = (await client.get(f"/api/runs/{run_id}/test-coverage")).json()
    assert summary["totalTests"] == summary["totalExecutions"] == summary["attributedRequests"] == 2
    assert summary["unattributedRequests"] == 0 and summary["statusCounts"]["failed"] == 1
    assert all("x-coverage-test-execution-id" not in request.headers for request in requests)


async def test_registration_result_retry_and_attempt_identity_are_idempotent(context):
    client, _, run_id, _ = context
    first, payload = await start(client, run_id)
    again = await client.post(f"/api/runs/{run_id}/tests", json=payload)
    assert again.json() == first
    assert (await client.post(f"/api/runs/{run_id}/tests", json={**payload, "name": "Different"})).status_code == 409
    assert (await client.post(f"/api/runs/{run_id}/tests", json={**payload, "id": str(uuid.uuid4())})).status_code == 409
    second, _ = await start(client, run_id, attempt=2)
    done = await finish(client, run_id, first["id"])
    assert (await finish(client, run_id, first["id"])).json() == done.json()
    assert (await finish(client, run_id, first["id"], "failed")).status_code == 409
    assert (await client.post(f"/api/runs/{run_id}/finish")).status_code == 200
    assert (await finish(client, run_id, first["id"])).status_code == 200
    missing = (await client.get(f"/api/runs/{run_id}/tests/{second['id']}")).json()
    assert missing["status"] == "running" and missing["resultMissing"]
    ended = await finish(client, run_id, second["id"], "failed", failureCode="teardown_failed")
    assert ended.status_code == 200 and ended.json()["collectionStatus"] == "completed"
    assert (await client.post(f"/api/runs/{run_id}/tests", json={**payload, "id": str(uuid.uuid4()), "attempt": 3})).status_code == 409


async def test_untagged_requests_and_unavailable_evidence_remain_explicit(context):
    client, _, run_id, _ = context
    assert (await exchange(client, run_id)).status_code == 200
    first, _ = await start(client, run_id)
    assert (await exchange(client, run_id, first["id"], "{ unknown }")).status_code == 200
    summary = (await client.get(f"/api/runs/{run_id}/test-coverage")).json()
    assert (summary["totalRequests"], summary["attributedRequests"], summary["unattributedRequests"]) == (2, 1, 1)
    detail = (await client.get(f"/api/runs/{run_id}/tests/{first['id']}")).json()
    assert detail["requestCount"] == 1 and detail["countedRequestCount"] == detail["coveredFieldCount"] == 0
    assert (await client.get(f"/api/runs/{run_id}/tests/{first['id']}/fields")).json() == []


async def test_mismatched_execution_is_rejected_before_upstream(context):
    client, sessions, run_id, requests = context
    first, _ = await start(client, run_id)
    async with sessions() as session:
        service = await create_service(session, "other-" + uuid.uuid4().hex, "Other", endpoint_url="https://93.184.216.34/graphql")
        schema = await import_sdl(session, service.id, SDL)
        other = await create_run(session, schema.id, "Other", {})
    assert (await exchange(client, str(other.id), first["id"])).status_code == 404
    assert (await exchange(client, run_id, str(uuid.uuid4()))).status_code == 404
    assert requests == []
    async with sessions() as session:
        session.add(ObservedRequest(run_id=other.id, test_execution_id=uuid.UUID(first["id"]), http_method="POST", analysis_status="unsupported"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_result_waits_for_capture_without_holding_a_database_connection(context, upstream):
    client, sessions, run_id, _ = context
    first, _ = await start(client, run_id)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(request):
        entered.set()
        await release.wait()
        return raw_response(body=BODY)

    upstream(slow)
    task = asyncio.create_task(exchange(client, run_id, first["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert sessions.kw["bind"].pool.checkedout() == 0
        result = await finish(client, run_id, first["id"])
        assert result.status_code == 409 and result.json()["detail"]["code"] == "test_requests_in_flight"
        active = (await client.get(f"/api/runs/{run_id}/tests/{first['id']}")).json()
        assert active["status"] == "running" and active["inFlightRequests"] == 1
    finally:
        release.set()
    assert (await task).status_code == 200
    result = await finish(client, run_id, first["id"])
    assert result.status_code == 200 and result.json()["inFlightRequests"] == 0


@pytest.mark.parametrize("error,status", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
async def test_failed_upstream_releases_the_test_slot(context, upstream, error, status):
    client, _, run_id, _ = context
    execution, _ = await start(client, run_id)

    def fail(request):
        raise error("test-only failure")

    upstream(fail)
    assert (await exchange(client, run_id, execution["id"])).status_code == status
    result = await finish(client, run_id, execution["id"], "failed")
    assert result.status_code == 200
    assert result.json()["inFlightRequests"] == result.json()["requestCount"] == 0


async def test_interrupted_execution_keeps_already_admitted_evidence(context, upstream):
    client, _, run_id, _ = context
    execution, _ = await start(client, run_id)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(request):
        entered.set()
        await release.wait()
        return raw_response(body=BODY)

    requests = upstream(slow)
    task = asyncio.create_task(exchange(client, run_id, execution["id"]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert (await finish(client, run_id, execution["id"], "interrupted")).status_code == 200
        assert (await exchange(client, run_id, execution["id"])).status_code == 409
    finally:
        release.set()
    assert (await task).status_code == 200 and len(requests) == 1
    detail = (await client.get(f"/api/runs/{run_id}/tests/{execution['id']}")).json()
    assert detail["status"] == "interrupted" and detail["requestCount"] == 1 and detail["inFlightRequests"] == 0


async def test_cancelled_request_releases_the_test_slot(context, upstream):
    client, _, run_id, _ = context
    execution, _ = await start(client, run_id)
    entered = asyncio.Event()

    async def slow(request):
        entered.set()
        await asyncio.Event().wait()

    upstream(slow)
    task = asyncio.create_task(exchange(client, run_id, execution["id"]))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await finish(client, run_id, execution["id"], "interrupted")
    assert result.status_code == 200
    assert result.json()["inFlightRequests"] == result.json()["requestCount"] == 0


async def test_public_service_does_not_publish_test_names(context):
    client, _, run_id, _ = context
    execution, _ = await start(client, run_id)
    services = (await client.get("/api/services")).json()
    assert len(services) == 1
    assert (await client.patch(f"/api/services/{services[0]['id']}", json={"isPublic": True})).status_code == 200
    for path in (f"/api/runs/{run_id}/tests", f"/api/runs/{run_id}/test-coverage",
                 f"/api/runs/{run_id}/tests/{execution['id']}/fields"):
        assert (await client.get(path, headers={"X-Coverage-Api-Key": ""})).status_code == 401
    assert (await client.get(f"/api/runs/{run_id}/coverage", headers={"X-Coverage-Api-Key": ""})).status_code == 200


@pytest.mark.parametrize("role,can_read,can_write", [("viewer", False, False), ("analyst", True, False), ("operator", True, True)])
async def test_test_coverage_respects_roles_grants_and_csrf(auth_context, postgres_session, role, can_read, can_write):
    client, users, private, _, _, _, run, _ = auth_context
    await grant(postgres_session, users[role], private)
    headers, _ = await login(client, role)
    route = f"/api/runs/{run.id}/tests"
    payload = {"id": str(uuid.uuid4()), "testId": "private::test", "name": "Private", "framework": "pytest"}
    assert (await client.get(route)).status_code == (200 if can_read else 403)
    response = await client.post(route, json=payload, headers=headers)
    assert response.status_code == (201 if can_write else 403)
    if can_write:
        assert (await client.post(route, json={**payload, "id": str(uuid.uuid4()), "attempt": 2})).status_code == 403


async def test_new_migration_preserves_historical_observations(isolated_database):
    sessions = isolated_database
    async with sessions() as session:
        service = await create_service(session, "old-" + uuid.uuid4().hex, "Historical")
        snapshot = await import_sdl(session, service.id, SDL)
        run = await create_run(session, snapshot.id, "Historical", {})
        row = ObservedRequest(run_id=run.id, http_method="POST", analysis_status="unsupported")
        session.add(row)
        await session.commit()
        ident = row.id
    engine = sessions.kw["bind"]
    url = engine.url.render_as_string(hide_password=False)

    async def migrate(*args):
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "alembic", *args],
            cwd=Path(__file__).resolve().parents[1], env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    await engine.dispose()
    await migrate("downgrade", "20261123_incomplete_collection")
    await migrate("upgrade", "head")
    await migrate("check")
    async with sessions() as session:
        restored = await session.get(ObservedRequest, ident)
        assert restored.run_id == run.id and restored.test_execution_id is None


@pytest.mark.parametrize("failure", ["exception", "database", "cancel"])
async def test_capture_failure_after_execution_lock_cannot_deadlock_cleanup(context, monkeypatch, failure):
    from app import services
    from app.models import TestExecution
    client, sessions, run_id, _ = context
    execution, _ = await start(client, run_id)
    original = services.record_observation
    locked = asyncio.Event()
    blocked = asyncio.Event()

    async def fail_after_lock(session, observation):
        # Use the actual PostgreSQL row lock, then fail before commit.
        await session.scalar(select(TestExecution).where(
            TestExecution.id == uuid.UUID(execution["id"])).with_for_update())
        locked.set()
        if failure == "cancel":
            await blocked.wait()
        elif failure == "database":
            await session.execute(text("SELECT 1 / 0"))
        else:
            raise RuntimeError("test-only capture failure")
        return await original(session, observation)

    monkeypatch.setattr(services, "record_observation", fail_after_lock)
    # e2e_capture imports record_observation directly.
    monkeypatch.setattr("app.e2e_capture.record_observation", fail_after_lock)
    task = asyncio.create_task(exchange(client, run_id, execution["id"]))
    await asyncio.wait_for(locked.wait(), 3)
    if failure == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
    else:
        assert (await asyncio.wait_for(task, 3)).status_code == 502
    async with sessions() as session:
        row = await session.get(TestExecution, uuid.UUID(execution["id"]))
        assert row.in_flight_requests == 0
        assert not (await session.scalars(select(ObservedRequest).where(
            ObservedRequest.test_execution_id == row.id))).all()
    assert sessions.kw["bind"].pool.checkedout() == 0
    assert (await finish(client, run_id, execution["id"], "failed")).status_code == 200


async def test_repeated_cancellation_during_cleanup_still_releases_slot(context, monkeypatch):
    from app import proxy
    client, sessions, run_id, _ = context
    execution, _ = await start(client, run_id)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = proxy.release_request

    async def delayed_release(sessions, execution_id):
        assert sessions.kw['bind'].pool.checkedout() == 0
        entered.set()
        await resume.wait()
        await original(sessions, execution_id)

    monkeypatch.setattr(proxy, 'release_request', delayed_release)
    task = asyncio.create_task(exchange(client, run_id, execution['id']))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    result = await finish(client, run_id, execution['id'])
    assert result.status_code == 200
    assert result.json()['inFlightRequests'] == 0 and result.json()['requestCount'] == 1
