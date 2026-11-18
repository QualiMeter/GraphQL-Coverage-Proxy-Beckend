import asyncio
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text

from test_test_coverage_postgres import context, exchange, finish
from test_graphql_proxy import upstream
from test_multi_service_postgres import isolated_database, routed_client
from test_browser_auth_postgres import auth_context, grant, login
from test_postgres_integration import postgres_session

pytestmark = pytest.mark.integration


async def register(client, run_id, *, test_id="cart", attempt=1, team="checkout", suite="smoke"):
    payload = {
        "id": str(uuid.uuid4()), "testId": test_id, "name": test_id, "framework": "pytest",
        "attempt": attempt, "teamKey": team, "suiteKey": suite,
    }
    response = await client.post(f"/api/runs/{run_id}/tests", json=payload)
    assert response.status_code == 201, response.text
    return response.json(), payload


async def test_groups_deduplicate_retries_and_keep_unassigned_requests(context):
    client, _, run_id, _ = context
    smoke, payload = await register(client, run_id)
    retry, _ = await register(client, run_id, attempt=2)
    regression, _ = await register(client, run_id, test_id="full", team="platform", suite="regression")
    legacy, _ = await register(client, run_id, test_id="old", team=None, suite=None)
    responses = await asyncio.gather(
        exchange(client, run_id, smoke["id"], "{ user { id } }"),
        exchange(client, run_id, retry["id"], "{ user { id } }"),
        exchange(client, run_id, regression["id"]),
        exchange(client, run_id, legacy["id"], "{ user { id } }"),
        exchange(client, run_id, None, "{ user { name } }"),
    )
    assert all(r.status_code == 200 for r in responses)
    await finish(client, run_id, smoke["id"], "failed")
    await finish(client, run_id, retry["id"])
    groups = await client.get(f"/api/runs/{run_id}/test-coverage/groups")
    assert groups.status_code == 200, groups.text
    assert groups.headers["X-Total-Count"] == "3"
    rows = {row["key"]: row for row in groups.json()}
    assert rows["smoke"]["totalTests"] == 1
    assert rows["smoke"]["totalExecutions"] == rows["smoke"]["requestCount"] == 2
    assert rows["smoke"]["coveredFieldCount"] == rows["smoke"]["observedFieldCount"] == 2
    assert rows["smoke"]["totalFields"] == 4 and rows["smoke"]["coveragePercent"] == 50
    assert rows["regression"]["coveredFieldCount"] == 3
    assert rows[None]["requestCount"] == 2 and rows[None]["totalTests"] == 1
    assert rows[None]["coveredFieldCount"] == 3
    fields = await client.get(f"/api/runs/{run_id}/test-coverage/fields", params={"suiteKey": "smoke"})
    assert {(row["typeName"], row["fieldName"], row["hits"]) for row in fields.json()} == {
        ("Query", "user", 2), ("User", "id", 2),
    }
    tests = await client.get(f"/api/runs/{run_id}/tests", params={"teamKey": "checkout", "suiteKey": "smoke"})
    assert len(tests.json()) == 2
    assert all(row["teamKey"] == "checkout" for row in tests.json())
    assert (await client.post(f"/api/runs/{run_id}/tests", json=payload)).status_code == 201
    assert (await client.post(f"/api/runs/{run_id}/tests", json={**payload, "teamKey": "other"})).status_code == 409
    page = await client.get(f"/api/runs/{run_id}/test-coverage/groups", params={"limit": 1, "offset": 1})
    assert len(page.json()) == 1 and page.headers["X-Total-Count"] == "3"
    teams = await client.get(f"/api/runs/{run_id}/test-coverage/groups", params={"groupBy": "team", "suiteKey": "smoke"})
    assert [row["key"] for row in teams.json()] == ["checkout"]
    unassigned = await client.get(f"/api/runs/{run_id}/tests", params={"unassignedSuite": True})
    assert [row["id"] for row in unassigned.json()] == [legacy["id"]]
    assert (await client.get(f"/api/runs/{run_id}/coverage")).json()["summary"]["coveredFields"] == 3
    assert (await client.get(f"/api/runs/{uuid.uuid4()}/test-coverage/groups")).status_code == 404


async def test_empty_groups_and_tests_without_requests(context):
    client, _, run_id, _ = context
    path = f"/api/runs/{run_id}/test-coverage/groups"
    assert (await client.get(path)).json() == []
    await register(client, run_id)
    row, = (await client.get(path)).json()
    assert row["totalTests"] == row["totalExecutions"] == 1
    assert row["requestCount"] == row["coveragePercent"] == row["observedFieldCount"] == 0
    assert (await client.get(path, params={"suiteKey": "unknown"})).json() == []
    assert (await client.get(path, params={"offset": 100})).json() == []
    fields = await client.get(f"/api/runs/{run_id}/test-coverage/fields", params={"offset": 100})
    assert fields.json() == [] and fields.headers["X-Total-Count"] == "0"
    for params in ({"teamKey": "x", "unassignedTeam": True}, {"groupBy": "invalid"},
                   {"offset": 2**63}, {"limit": 501}):
        assert (await client.get(path, params=params)).status_code == 422


async def test_count_and_group_page_agree_during_registration(context, monkeypatch):
    from app.test_executions import get_run
    from app.test_groups import GroupPageFilters, group_coverage
    client, sessions, run_id, _ = context
    await register(client, run_id)
    async with sessions() as session:
        run = await get_run(session, uuid.UUID(run_id))
        scalar = session.scalar
        registered = False

        async def register_between_reads(*args, **kwargs):
            nonlocal registered
            result = await scalar(*args, **kwargs)
            if not registered:
                registered = True
                await register(client, run_id, test_id="another", suite="regression")
            return result

        monkeypatch.setattr(session, "scalar", register_between_reads)
        rows, total = await group_coverage(session, run, GroupPageFilters())
        assert total == len(rows) == 2


async def test_count_and_field_page_agree_during_capture(context, monkeypatch):
    from app.test_executions import execution_fields
    from app.test_groups import GroupFilters
    client, sessions, run_id, _ = context
    execution, _ = await register(client, run_id)
    assert (await exchange(client, run_id, execution["id"], "{ user { id } }")).status_code == 200
    async with sessions() as session:
        execute = session.execute
        captured = False

        async def capture_before_page(*args, **kwargs):
            nonlocal captured
            if not captured:
                captured = True
                assert (await exchange(client, run_id, execution["id"])).status_code == 200
            return await execute(*args, **kwargs)

        monkeypatch.setattr(session, "execute", capture_before_page)
        rows, total = await execution_fields(
            session, uuid.UUID(run_id), None, groups=GroupFilters(suite_key="smoke"),
        )
        assert total == len(rows) == 3
    page = await client.get(f"/api/runs/{run_id}/test-coverage/fields", params={"offset": 100})
    assert page.json() == [] and page.headers["X-Total-Count"] == "3"


async def test_same_suite_in_other_snapshot_does_not_change_group_coverage(context):
    from app.models import TestRun as Run, SchemaSnapshot
    from app.services import create_run, import_sdl
    client, sessions, run_id, _ = context
    first, _ = await register(client, run_id)
    assert (await exchange(client, run_id, first["id"], "{ user { id } }")).status_code == 200
    async with sessions() as session:
        run = await session.get(Run, uuid.UUID(run_id))
        snapshot = await session.get(SchemaSnapshot, run.snapshot_id)
        other_schema = await import_sdl(session, snapshot.service_id, "type Query { other: String }")
        other_run = await create_run(session, other_schema.id, "Other snapshot", {})
    other, _ = await register(client, str(other_run.id))
    assert (await exchange(client, str(other_run.id), other["id"], "{ other }")).status_code == 200
    first_row, = (await client.get(f"/api/runs/{run_id}/test-coverage/groups")).json()
    other_row, = (await client.get(f"/api/runs/{other_run.id}/test-coverage/groups")).json()
    assert first_row["totalFields"] == 4 and first_row["coveredFieldCount"] == 2
    assert other_row["totalFields"] == other_row["coveredFieldCount"] == 1
    assert first_row["requestCount"] == other_row["requestCount"] == 1


async def test_group_migration_preserves_existing_executions_and_requests(context):
    client, sessions, run_id, _ = context
    execution, _ = await register(client, run_id)
    assert (await exchange(client, run_id, execution["id"])).status_code == 200
    url = sessions.kw["bind"].url.render_as_string(hide_password=False)

    async def migrate(*args):
        result = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-m", "alembic", *args],
            cwd=Path(__file__).resolve().parents[1], env={**os.environ, "DATABASE_URL": url},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    await migrate("downgrade", "20261124_test_executions")
    async with sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM test_executions")) == 1
        assert await session.scalar(text("SELECT count(*) FROM observed_requests")) == 1
    await migrate("upgrade", "head")
    await migrate("check")
    detail = (await client.get(f"/api/runs/{run_id}/tests/{execution['id']}")).json()
    assert detail["teamKey"] is None and detail["suiteKey"] is None
    row, = (await client.get(f"/api/runs/{run_id}/test-coverage/groups")).json()
    assert row["key"] is None and row["coveredFieldCount"] == 3
    assert (await client.get(f"/api/runs/{run_id}/coverage")).json()["summary"]["coveredFields"] == 3


@pytest.mark.parametrize("role,status", [("viewer", 403), ("analyst", 200), ("operator", 200), ("admin", 200)])
async def test_group_routes_enforce_service_grants(auth_context, postgres_session, role, status):
    client, users, private, _, _, _, run, _ = auth_context
    await login(client, role)
    if role != "admin":
        expected = 403 if role == "viewer" else 404
        for suffix in ("groups", "fields"):
            assert (await client.get(f"/api/runs/{run.id}/test-coverage/{suffix}")).status_code == expected
    await grant(postgres_session, users[role], private)
    for suffix in ("groups", "fields"):
        assert (await client.get(f"/api/runs/{run.id}/test-coverage/{suffix}")).status_code == status
