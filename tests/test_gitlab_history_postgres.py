import asyncio
import os
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.auth import hash_password
from app.auth_models import ServiceGrant, User
from app.database import get_session, get_session_factory
from app.gitlab_client import GitLabClient, GitLabError, JobInfo, PipelineInfo
from app.gitlab_config import GitLabSettings
from app.gitlab_history import sync_jobs_once
from app.gitlab_runs import apply_result, claim, now, prepare_launch
from app.main import app
from app.models import GitLabJob, GitLabPipeline, TestRun as Run
from app.services import create_run, create_service, finish_run, import_sdl
from test_browser_auth_postgres import ORIGIN, PASSWORD, login
from test_graphql_proxy import TEST_ADMIN_KEY
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration
SHA = "a" * 40


def pipeline(ident, branch="main", status="failed", sha=SHA):
    return PipelineInfo(ident, f"https://gitlab.example/qa/-/pipelines/{ident}", status, sha, branch)


def job(ident, name="api", status="failed"):
    return JobInfo(ident, name, "test", status, f"https://gitlab.example/qa/-/jobs/{ident}",
                   now(), None, None)


@pytest.fixture
async def history(isolated_database, monkeypatch):
    sessions = isolated_database
    async with sessions() as session:
        service = await create_service(session, "ci-history", "CI History", is_public=True)
        snapshot = await import_sdl(session, service.id, "type Query { health: String }")
    settings = GitLabSettings(
        "https://gitlab.example", "private-token", {service.id: 45},
        "https://coverage.example", environments={service.id: "qa/staging"},
    )
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_ADMIN_KEY)
    monkeypatch.setenv("COVERAGE_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.delenv("COVERAGE_DEV_MODE", raising=False)

    async def request_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = request_session
    app.dependency_overrides[get_session_factory] = lambda: sessions
    try:
        yield sessions, service, snapshot, settings
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_session_factory, None)


async def prepare(history, branch="main"):
    sessions, service, snapshot, settings = history
    async with sessions() as session:
        return await prepare_launch(session, settings, service.id, snapshot.id, branch, None)


async def terminal_run(history, ident, jobs, *, branch="main", settings=None):
    sessions, service, snapshot, base_settings = history
    settings = settings or base_settings
    item = await prepare((sessions, service, snapshot, settings), branch)
    await apply_result(sessions, item, settings, info=pipeline(ident, branch))
    client = AsyncMock(spec=GitLabClient)
    client.jobs.return_value = jobs, False
    assert await sync_jobs_once(sessions, settings, client)
    client.jobs.assert_awaited_once_with(45, ident, 1)
    return item.run_id


async def test_history_filter_sort_and_compare_with_distinct_ci_status(history):
    sessions, service, snapshot, settings = history
    a = await terminal_run(history, 100, [job(11, "api"), job(12, "api", "success")])
    b = await terminal_run(history, 200, [job(21, "api")])
    await terminal_run(history, 300, [job(31)], branch="another")
    await terminal_run(history, 400, [job(41)], settings=replace(settings, environments={service.id: "qa/demo"}))
    async with sessions() as session:
        manual = await create_run(session, snapshot.id, "Manual", {
            "ci": {"branch": "main", "pipelineId": 100, "commitSha": SHA},
            "branch": "main", "environment": "qa/staging",
        })
        await finish_run(session, manual.id)
        first_start = (await session.get(Run, a)).started_at
        last_start = (await session.get(Run, b)).started_at
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://graphql.rebyte.fun",
                                 headers={"X-Coverage-Api-Key": TEST_ADMIN_KEY}) as client:
        response = await client.get("/api/runs", params={
            "serviceId": str(service.id), "from": first_start.isoformat(),
            "to": (last_start + timedelta(seconds=60)).isoformat(), "branch": "main",
            "commitSha": SHA, "environment": "qa/staging", "jobName": "api", "jobStatus": "failed",
            "pipelineStatus": "failed", "status": "completed", "sortBy": "jobId",
        })
        assert response.status_code == 200, response.text
        assert response.headers["X-Total-Count"] == "2"
        rows = response.json()
        assert [row["id"] for row in rows] == [str(b), str(a)]
        assert rows[1]["ci"] == {
            "provider": "gitlab", "projectId": 45, "branch": "main", "commitSha": SHA,
            "environment": "qa/staging", "pipelineId": 100,
            "pipelineUrl": "https://gitlab.example/qa/-/pipelines/100", "pipelineStatus": "failed",
            "jobsSyncedAt": rows[1]["ci"]["jobsSyncedAt"], "jobsFinalized": True,
        }
        response = await client.get("/api/runs", params={
            "snapshotId": str(snapshot.id), "sortBy": "pipelineId", "sortOrder": "desc",
            "limit": 1, "offset": 4,
        })
        assert response.headers["X-Total-Count"] == "5"
        row, = response.json()
        assert row["id"] == str(manual.id) and row["ci"] is None
        response = await client.get(f"/api/runs/{a}/gitlab/jobs", params={"limit": 1, "offset": 1})
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 2 and response.json()["jobsFinalized"]
        item, = response.json()["jobs"]
        assert item["jobId"] == 11 and item["url"].endswith("/-/jobs/11") and item["stage"] == "test"
        assert "private-token" not in response.text
        response = await client.post("/api/runs/compare", json={
            "runIds": [row["id"] for row in rows], "baselineRunId": str(a),
        })
        assert response.status_code == 200, response.text
        assert await _matching_count(client, snapshot.id, pipelineId=100, jobId=12, jobStatus="failed") == 0
        assert await _matching_count(client, snapshot.id, pipelineId=100, jobId=12, jobStatus="success") == 1
        response = await client.get("/api/runs", params={"snapshotId": str(snapshot.id), "branch": "\x00"})
        assert response.status_code == 422


async def _matching_count(client, snapshot_id, **filters):
    response = await client.get("/api/runs", params={"snapshotId": str(snapshot_id), **filters})
    assert response.status_code == 200, response.text
    return int(response.headers["X-Total-Count"])


async def test_ci_metadata_requires_same_acl_as_gitlab_status(history):
    sessions, service, snapshot, _ = history
    run_id = await terminal_run(history, 100, [job(11)])
    async with sessions() as session:
        viewer = User(username="ci-viewer", role="viewer", password_hash=hash_password(PASSWORD))
        session.add(viewer)
        await session.commit()
        viewer_id = viewer.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://graphql.rebyte.fun") as client:
        params = {"snapshotId": str(snapshot.id)}
        url = f"/api/runs/{run_id}/gitlab/jobs"
        assert (await client.get("/api/runs", params=params)).json()[0]["ci"] is None
        assert (await client.get("/api/runs", params={**params, "sortBy": "jobName"})).status_code == 404
        assert (await client.get(url)).status_code == 401
        await login(client, "ci-viewer")
        assert (await client.get("/api/runs", params=params)).json()[0]["ci"] is None
        assert (await client.get(url)).status_code == 404
        async with sessions() as session:
            session.add(ServiceGrant(user_id=viewer_id, service_id=service.id))
            await session.commit()
        assert (await client.get("/api/runs", params={**params, "branch": "main"})).json()[0]["ci"]["pipelineId"] == 100
        assert (await client.get(url)).json()["jobs"][0]["jobId"] == 11


async def test_job_sync_keeps_partial_history_on_error_and_finishes_after_restart(history):
    sessions, _, _, settings = history
    item = await prepare(history)
    await apply_result(sessions, item, settings, info=pipeline(100, status="pending", sha=None))
    client = AsyncMock(spec=GitLabClient)
    client.pipeline.return_value = pipeline(100, status="pending")
    client.jobs.return_value = [job(11, status="pending")], True
    assert await sync_jobs_once(sessions, settings, client)
    client.jobs.side_effect = GitLabError("gitlab_unavailable")
    assert await sync_jobs_once(sessions, settings, client)
    async with sessions.begin() as session:
        row = await session.get(GitLabPipeline, item.run_id)
        assert row.commit_sha == SHA and row.jobs_page == 2 and row.jobs_synced_at is None
        assert (await session.get(GitLabJob, (item.run_id, 11))).status == "pending"
        assert (await session.get(Run, item.run_id)).status == "running"
        row.next_check_at = now()
    tracking = await claim(sessions)
    assert tracking.run_id == item.run_id
    await apply_result(sessions, tracking, settings, info=pipeline(100))

    restarted = AsyncMock(spec=GitLabClient)
    restarted.jobs.return_value = [job(11), job(12, "retry")], False
    assert await sync_jobs_once(sessions, settings, restarted)
    restarted.jobs.assert_awaited_once_with(45, 100, 1)
    restarted.pipeline.assert_not_awaited()
    async with sessions() as session:
        row = await session.get(GitLabPipeline, item.run_id)
        assert row.jobs_next_check_at is None and row.jobs_synced_at is not None
        assert (await session.get(Run, item.run_id)).status == "completed"
        jobs = (await session.scalars(select(GitLabJob).order_by(GitLabJob.job_id))).all()
        assert [(entry.job_id, entry.status) for entry in jobs] == [(11, "failed"), (12, "failed")]


async def test_upgrade_schedules_legacy_pipeline_without_inventing_provenance(history):
    sessions, _, _, settings = history
    item = await prepare(history)
    await apply_result(sessions, item, settings, info=pipeline(123))
    async with sessions() as session:
        url = session.bind.url.render_as_string(hide_password=False)

    async def migrate(*arguments):
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "alembic", *arguments],
                                         env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    await migrate("downgrade", "20261124_test_executions")
    await migrate("upgrade", "head")
    async with sessions() as session:
        row = await session.get(GitLabPipeline, item.run_id)
        assert row.pipeline_id == 123 and row.pipeline_status == "failed"
        assert row.commit_sha is None and row.environment is None
        assert row.jobs_next_check_at is not None and row.jobs_synced_at is None
    client = AsyncMock(spec=GitLabClient)
    client.pipeline.return_value = pipeline(123)
    client.jobs.return_value = [], False
    assert await sync_jobs_once(sessions, settings, client)
    async with sessions() as session:
        row = await session.get(GitLabPipeline, item.run_id)
        assert row.commit_sha == SHA and row.environment is None and row.jobs_next_check_at is None
