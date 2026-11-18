import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app import gitlab_api, gitlab_runs
from app.auth import Principal, csrf_token
from app.auth_models import ServiceGrant, User
from app.contracts import AnalysisResult, FieldReference, RequestObservationInput
from app.database import get_session_factory
from app.gitlab_client import GitLabClient, GitLabError, PipelineInfo
from app.gitlab_config import GitLabSettings
from app.gitlab_runs import apply_result, claim, launch, now, poll_once, prepare_launch
from app.main import app
from app.models import GitLabPipeline, TestRun as Run
from app.services import NotFoundError, ConflictError, create_run, create_service, finish_run, get_run_graph, import_sdl, record_observation
from test_postgres_integration import postgres_session  # noqa: F401
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
async def setup(postgres_session, monkeypatch):
    session = postgres_session
    service = await create_service(session, "gitlab-" + uuid.uuid4().hex, "GitLab test")
    snapshot = await import_sdl(session, service.id, "type Query { health: String }")
    config = GitLabSettings("https://gitlab.example", "test-only-gitlab-token", {service.id: 123},
                            "https://coverage.example/coverage", poll_seconds=2, max_run_seconds=60)
    active_sessions = set()

    @asynccontextmanager
    async def sessions():
        async with AsyncSession(bind=session.bind, expire_on_commit=False,
                                join_transaction_mode="create_savepoint") as current:
            active_sessions.add(current)
            try:
                yield current
            finally:
                active_sessions.remove(current)

    monkeypatch.setattr(gitlab_api, "settings", lambda: config)
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[gitlab_api.gitlab_principal] = lambda: Principal(role="admin", api_key=True)
    try:
        yield session, sessions, service, snapshot, config, active_sessions
    finally:
        app.dependency_overrides.pop(get_session_factory, None)
        app.dependency_overrides.pop(gitlab_api.gitlab_principal, None)


def info(status="pending"):
    return PipelineInfo(42, "https://gitlab.example/demo/-/pipelines/42", status)


async def prepare(setup):
    _, sessions, service, snapshot, config, _ = setup
    async with sessions() as session:
        return await prepare_launch(session, config, service.id, snapshot.id, "main", None)


async def due(sessions, item, *, expired=False):
    async with sessions() as session:
        row = await session.get(GitLabPipeline, item.run_id)
        row.next_check_at = now() - timedelta(seconds=1)
        row.lease_until = now() - timedelta(seconds=1)
        if expired:
            row.deadline_at = now() - timedelta(seconds=1)
        await session.commit()


@pytest.mark.parametrize("final", ["success", "failed", "canceled", "skipped"])
async def test_pipeline_completion_keeps_coverage_and_is_idempotent(setup, final):
    session, sessions, _, snapshot, config, active = setup
    item = await prepare(setup)
    await record_observation(session, RequestObservationInput(
        run_id=item.run_id, analysis=AnalysisResult(status="success", operation_type="query",
        fields=(FieldReference("Query", "health"),)), http_method="POST", http_status=200,
        response_has_data=True, graphql_has_errors=False,
    ))
    client = AsyncMock(spec=GitLabClient)
    async def send(*args):
        assert active == set(), "A DB session survived across network I/O"
        return info()
    client.launch.side_effect = send
    assert await launch(sessions, config, item, client) is None
    async with sessions() as s:
        with pytest.raises(ConflictError, match="managed by GitLab"):
            await finish_run(s, item.run_id)
    await due(sessions, item)
    async def status(*args):
        assert active == set()
        return info(final)
    client.pipeline.side_effect = status
    assert await poll_once(sessions, config, client)
    async with sessions() as s:
        run = await s.get(Run, item.run_id)
        row = await s.get(GitLabPipeline, item.run_id)
        finished = run.finished_at
        assert run.status == "completed" and row.pipeline_status == final
        assert row.finished_at is not None and not row.collection_incomplete
        graph = await get_run_graph(s, item.run_id)
        assert graph.summary.covered_fields == 1
    await apply_result(sessions, item, config, info=info(final))
    assert not await poll_once(sessions, config, client)
    async with sessions() as s:
        assert (await s.get(Run, item.run_id)).finished_at == finished
        # Terminal pipelines release the snapshot for a new manual run.
        await create_run(s, snapshot.id, "next", {})
    assert client.launch.await_count == 1


@pytest.mark.parametrize("code", ["gitlab_rejected", "gitlab_unauthorized", "gitlab_forbidden", "gitlab_not_found", "gitlab_destination_unavailable"])
async def test_definitive_rejection_fails_run_and_releases_snapshot(setup, code):
    _, sessions, _, snapshot, config, _ = setup
    item = await prepare(setup)
    client = AsyncMock(spec=GitLabClient)
    client.launch.side_effect = GitLabError(code)
    error = await launch(sessions, config, item, client)
    assert error.code == code
    async with sessions() as s:
        run, row = await s.get(Run, item.run_id), await s.get(GitLabPipeline, item.run_id)
        assert run.status == "failed" and run.finished_at is not None
        assert row.error_code == code and row.finished_at is not None
        await create_run(s, snapshot.id, "retry", {})


async def test_ambiguous_launch_recovers_after_restart_without_second_post(setup):
    _, sessions, _, _, config, _ = setup
    item = await prepare(setup)
    first = AsyncMock(spec=GitLabClient)
    first.launch.side_effect = GitLabError("gitlab_network_error", ambiguous=True)
    await launch(sessions, config, item, first)
    async with sessions() as s:
        assert (await s.get(Run, item.run_id)).status == "running"
        assert (await s.get(GitLabPipeline, item.run_id)).pipeline_status == "launch_unknown"
    await due(sessions, item)
    restarted_client = AsyncMock(spec=GitLabClient)
    restarted_client.reconcile.return_value = info("failed"), 1
    assert await poll_once(sessions, config, restarted_client)
    restarted_client.launch.assert_not_called()
    async with sessions() as s:
        assert (await s.get(Run, item.run_id)).status == "completed"
        assert (await s.get(GitLabPipeline, item.run_id)).pipeline_id == 42


async def test_crash_between_intent_commit_and_post_is_never_automatically_relaunched(setup):
    _, sessions, _, _, config, _ = setup
    item = await prepare(setup)
    assert await claim(sessions) is None  # live launch lease
    await due(sessions, item)
    client = AsyncMock(spec=GitLabClient)
    client.reconcile.return_value = None, 2
    assert await poll_once(sessions, config, client)
    client.launch.assert_not_called()
    async with sessions() as s:
        row = await s.get(GitLabPipeline, item.run_id)
        assert row.reconcile_page == 2 and row.finished_at is None


@pytest.mark.parametrize("known", [True, False])
async def test_deadline_stops_collection_but_quarantines_snapshot_until_reconciled(setup, known):
    _, sessions, _, snapshot, config, _ = setup
    item = await prepare(setup)
    if known:
        await apply_result(sessions, item, config, info=info())
    await due(sessions, item, expired=True)
    client = AsyncMock(spec=GitLabClient)
    client.pipeline.side_effect = GitLabError("gitlab_network_error")
    client.reconcile.side_effect = GitLabError("gitlab_network_error")
    await poll_once(sessions, config, client)
    async with sessions() as s:
        assert (await s.get(Run, item.run_id)).status == "failed"
        row = await s.get(GitLabPipeline, item.run_id)
        assert row.error_code == "tracking_deadline_exceeded" and row.finished_at is None
        assert row.collection_incomplete
        with pytest.raises(ConflictError):
            await create_run(s, snapshot.id, "unsafe duplicate", {})
    await due(sessions, item)
    client.pipeline.side_effect = None
    client.pipeline.return_value = info("success")
    client.reconcile.side_effect = None
    client.reconcile.return_value = info("success"), 1
    await poll_once(sessions, config, client)
    async with sessions() as s:
        row = await s.get(GitLabPipeline, item.run_id)
        assert row.finished_at is not None and row.collection_incomplete
        assert row.error_code == "tracking_deadline_exceeded"
        assert (await s.get(Run, item.run_id)).status == "failed"
        await create_run(s, snapshot.id, "safe next", {})


@pytest.mark.parametrize("status", ["manual", "scheduled", "preparing", "waiting_for_resource", "unknown_future"])
async def test_nonterminal_status_does_not_close_collection(setup, status):
    _, sessions, _, _, config, _ = setup
    item = await prepare(setup)
    await apply_result(sessions, item, config, info=info(status))
    async with sessions() as s:
        assert (await s.get(Run, item.run_id)).status == "running"
        assert (await s.get(GitLabPipeline, item.run_id)).pipeline_status == status


async def test_stale_worker_cannot_overwrite_new_lease(setup):
    _, sessions, _, _, config, _ = setup
    item = await prepare(setup)
    await due(sessions, item)
    newer = await claim(sessions)
    assert newer.lease != item.lease
    await apply_result(sessions, item, config, info=info("success"))
    async with sessions() as s:
        assert (await s.get(Run, item.run_id)).status == "running"
    await apply_result(sessions, newer, config, info=info("failed"))
    async with sessions() as s:
        assert (await s.get(Run, item.run_id)).status == "completed"


async def test_intent_insert_failure_rolls_back_run_atomically(setup):
    session, sessions, _, snapshot, _, _ = setup
    await session.execute(text("ALTER TABLE gitlab_pipelines ADD CONSTRAINT test_reject_gitlab CHECK (false) NOT VALID"))
    await session.commit()
    with pytest.raises(SQLAlchemyError):
        await prepare(setup)
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Run).where(Run.snapshot_id == snapshot.id)) == 0


async def test_db_failure_after_remote_creation_is_reconciled_not_reposted(setup, monkeypatch):
    _, sessions, _, _, config, _ = setup
    item = await prepare(setup)
    client = AsyncMock(spec=GitLabClient)
    client.launch.return_value = info()
    original = gitlab_runs.apply_result
    monkeypatch.setattr(gitlab_runs, "apply_result", AsyncMock(side_effect=SQLAlchemyError("test failure")))
    with pytest.raises(SQLAlchemyError):
        await launch(sessions, config, item, client)
    monkeypatch.setattr(gitlab_runs, "apply_result", original)
    await due(sessions, item)
    client.reconcile.return_value = info("success"), 1
    await poll_once(sessions, config, client)
    assert client.launch.await_count == 1
    async with sessions() as s:
        assert (await s.get(Run, item.run_id)).status == "completed"


async def test_launch_and_status_endpoints_and_active_conflict(setup, monkeypatch):
    _, sessions, service, snapshot, _, active = setup
    posts = []
    async def branch(self, *args):
        assert not active
    async def send(self, project, ref, run_id):
        assert not active
        posts.append(run_id)
        return info()
    monkeypatch.setattr(GitLabClient, "check_branch", branch)
    monkeypatch.setattr(GitLabClient, "launch", send)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://graphql.rebyte.fun") as client:
        body = {"serviceId": str(service.id), "snapshotId": str(snapshot.id), "ref": "main"}
        response = await client.post("/api/runs/gitlab", json=body)
        assert response.status_code == 202, response.text
        result = response.json()
        assert uuid.UUID(result["runId"]) == posts[0]
        assert result["pipelineId"] == 42 and result["pipelineStatus"] == "pending"
        assert result["runStatus"] == "running"
        assert (await client.post("/api/runs/gitlab", json=body)).status_code == 409
        assert len(posts) == 1
        path = f'/api/runs/{result["runId"]}/gitlab'
        assert (await client.get(path)).json() == result
        # Unknown service access is checked before any GitLab network operation.
        body["serviceId"] = str(uuid.uuid4())
        assert (await client.post("/api/runs/gitlab", json=body)).status_code == 404
        assert (await client.get(f"/api/runs/{uuid.uuid4()}/gitlab")).status_code == 404


async def test_snapshot_must_belong_to_selected_service(setup):
    session, _, _, snapshot, config, _ = setup
    other = await create_service(session, "other-" + uuid.uuid4().hex, "Other")
    with pytest.raises(NotFoundError):
        await prepare_launch(session, replace(config, projects={other.id: 123}), other.id, snapshot.id, "main", None)


async def test_service_grants_roles_csrf_and_validation(setup, monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", "test-only-coverage-key-" + "x" * 40)
    session, _, service, snapshot, _, _ = setup
    branch = AsyncMock()
    monkeypatch.setattr(GitLabClient, "check_branch", branch)
    user_id = uuid.uuid4()
    # Unauthorized principals must fail without reaching GitLab or creating a run.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://graphql.rebyte.fun") as client:
        payload = {"serviceId": str(service.id), "snapshotId": str(snapshot.id), "ref": "main"}
        for principal, expected in [(Principal(), 401), (Principal(user_id, "viewer"), 403),
                                    (Principal(user_id, "operator", api_key=True), 404)]:
            app.dependency_overrides[gitlab_api.gitlab_principal] = lambda p=principal: p
            response = await client.post("/api/runs/gitlab", json=payload)
            assert response.status_code == expected, response.text
        app.dependency_overrides[gitlab_api.gitlab_principal] = lambda: Principal(user_id, "operator", token="a" * 43)
        assert (await client.post("/api/runs/gitlab", json=payload)).status_code == 403
        assert (await client.post("/api/runs/gitlab", json=payload, headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf_token("a" * 43)})).status_code == 403
        branch.assert_not_called()
        app.dependency_overrides[gitlab_api.gitlab_principal] = lambda: Principal(role="admin", api_key=True)
        assert (await client.post("/api/runs/gitlab", json={**payload, "token": "never-echo-this"})).status_code == 422
        response = await client.post("/api/runs/gitlab", json={**payload, "host": "https://evil.example/never-echo-this"})
        assert response.status_code == 422 and "never-echo-this" not in response.text
        assert (await client.post("/api/runs/gitlab", content=b"x" * 17000)).status_code == 413

        session.add(User(id=user_id, username="gitlab-" + uuid.uuid4().hex, role="operator", password_hash="unused-test-hash"))
        await session.flush()
        session.add(ServiceGrant(user_id=user_id, service_id=service.id))
        await session.commit()
        app.dependency_overrides[gitlab_api.gitlab_principal] = lambda: Principal(user_id, "operator", token="a" * 43)
        monkeypatch.setattr(GitLabClient, "launch", AsyncMock(return_value=info()))
        response = await client.post("/api/runs/gitlab", json=payload, headers={
            "Origin": "https://graphql.rebyte.fun", "X-CSRF-Token": csrf_token("a" * 43),
        })
        assert response.status_code == 202, response.text
        run_id = response.json()["runId"]
        app.dependency_overrides[gitlab_api.gitlab_principal] = lambda: Principal(user_id, "viewer")
        assert (await client.get(f"/api/runs/{run_id}/gitlab")).status_code == 200


async def test_status_is_not_public_even_for_published_service(setup):
    session, _, service, _, _, _ = setup
    item = await prepare(setup)
    app.dependency_overrides[gitlab_api.gitlab_principal] = lambda: Principal(uuid.uuid4(), "viewer")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://graphql.rebyte.fun") as client:
        assert (await client.get(f"/api/runs/{item.run_id}/gitlab")).status_code == 404


async def test_concurrent_http_launch_and_workers_use_one_pipeline(isolated_database, monkeypatch):
    sessions = isolated_database
    async with sessions() as s:
        service = await create_service(s, "concurrent-gitlab", "Concurrent GitLab")
        snapshot = await import_sdl(s, service.id, "type Query { health: String }")
    config = GitLabSettings("https://gitlab.example", "test-token", {service.id: 123}, "https://coverage.example")
    monkeypatch.setattr(gitlab_api, "settings", lambda: config)
    monkeypatch.setattr(GitLabClient, "check_branch", AsyncMock())
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def send(self, project, ref, run_id):
        calls.append(run_id)
        started.set()
        await release.wait()
        return info()
    monkeypatch.setattr(GitLabClient, "launch", send)
    overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_session_factory] = lambda: sessions
    app.dependency_overrides[gitlab_api.gitlab_principal] = lambda: Principal(role="admin", api_key=True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://graphql.rebyte.fun") as client:
            payload = {"serviceId": str(service.id), "snapshotId": str(snapshot.id), "ref": "main"}
            first = asyncio.create_task(client.post("/api/runs/gitlab", json=payload))
            await asyncio.wait_for(started.wait(), 5)
            second = await asyncio.wait_for(client.post("/api/runs/gitlab", json=payload), 5)
            assert second.status_code == 409
            release.set()
            assert (await asyncio.wait_for(first, 5)).status_code == 202
            assert len(calls) == 1
        async with sessions() as s:
            assert await s.scalar(select(func.count()).select_from(Run)) == 1
        item = await _work_for_run(sessions, calls[0])
        await due(sessions, item)
        claims = await asyncio.gather(claim(sessions), claim(sessions))
        assert sum(value is not None for value in claims) == 1
        claimed = next(value for value in claims if value is not None)
        await asyncio.gather(apply_result(sessions, claimed, config, info=info("success")),
                             apply_result(sessions, claimed, config, info=info("success")))
        async with sessions() as s:
            assert (await s.get(Run, calls[0])).status == "completed"
    finally:
        release.set()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


async def _work_for_run(sessions, run_id):
    async with sessions() as session:
        return gitlab_runs.work(await session.get(GitLabPipeline, run_id))


async def test_lifespan_restores_polling_without_browser(isolated_database, monkeypatch):
    import app.main as main
    sessions = isolated_database
    async with sessions() as session:
        service = await create_service(session, "restart-gitlab", "Restart")
        snapshot = await import_sdl(session, service.id, "type Query { health: String }")
        config = GitLabSettings("https://gitlab.example", "test-token", {service.id: 123},
                                "https://coverage.example", poll_seconds=2)
        item = await prepare_launch(session, config, service.id, snapshot.id, "main", None)
    await apply_result(sessions, item, config, info=info())
    await due(sessions, item)
    checked = asyncio.Event()
    async def status(*args):
        checked.set()
        return info("failed")
    monkeypatch.setattr(GitLabClient, "pipeline", status)
    monkeypatch.setattr(main, "SessionFactory", sessions)
    monkeypatch.setattr(main, "gitlab_enabled", lambda: True)
    monkeypatch.setattr(main, "gitlab_settings", lambda: config)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(checked.wait(), 5)
        for _ in range(100):
            async with sessions() as session:
                if (await session.get(Run, item.run_id)).status == "completed":
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Background poller did not finalize the run")


@pytest.mark.parametrize("stage,ambiguous,expected_status,run_status", [
    ("branch", False, 502, "failed"), ("post", False, 502, "failed"),
    ("post", True, 202, "running"),
])
async def test_http_launch_errors_preserve_recoverable_run_id(setup, monkeypatch, stage, ambiguous, expected_status, run_status):
    _, sessions, service, snapshot, _, _ = setup
    monkeypatch.setattr(GitLabClient, "check_branch", AsyncMock(side_effect=GitLabError("gitlab_not_found") if stage == "branch" else None))
    post = AsyncMock(side_effect=GitLabError("gitlab_network_error" if ambiguous else "gitlab_rejected", ambiguous=ambiguous))
    monkeypatch.setattr(GitLabClient, "launch", post)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://graphql.rebyte.fun") as client:
        response = await client.post("/api/runs/gitlab", json={"serviceId": str(service.id), "snapshotId": str(snapshot.id), "ref": "main"})
        assert response.status_code == expected_status, response.text
        body = response.json() if expected_status == 202 else response.json()["detail"]
        run_id = uuid.UUID(body["runId"])
        async with sessions() as session:
            assert (await session.get(Run, run_id)).status == run_status
    if stage == "branch":
        post.assert_not_called()


async def test_branch_list_is_paginated_and_authorized_before_http(setup, monkeypatch):
    _, _, service, _, _, active = setup
    calls = []
    async def branches(self, project, page):
        assert not active
        calls.append((project, page))
        return ["main", "feat/qa-platform-and-demo-shop"]
    monkeypatch.setattr(GitLabClient, "branches", branches)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://graphql.rebyte.fun") as client:
        response = await client.get(f"/api/services/{service.id}/gitlab/branches?page=2")
        assert response.json() == ["main", "feat/qa-platform-and-demo-shop"]
        assert calls == [(123, 2)]
        assert (await client.get(f"/api/services/{uuid.uuid4()}/gitlab/branches")).status_code == 404
        assert calls == [(123, 2)]


async def test_host_change_does_not_send_token_to_persisted_host(setup):
    _, sessions, _, _, config, _ = setup
    item = await prepare(setup)
    await due(sessions, item)
    client = AsyncMock(spec=GitLabClient)
    await poll_once(sessions, replace(config, host="https://other-gitlab.example"), client)
    client.reconcile.assert_not_called()
    client.pipeline.assert_not_called()
    async with sessions() as session:
        row = await session.get(GitLabPipeline, item.run_id)
        assert row.error_code == "gitlab_configuration_changed"
        assert row.check_failures == 1
        assert row.next_check_at > now()


async def test_operator_absence_resolution_is_guarded_and_releases_quarantine(setup, monkeypatch):
    from app import gitlab_cli
    _, sessions, _, snapshot, config, _ = setup
    monkeypatch.setattr(gitlab_cli, "SessionFactory", sessions)
    item = await prepare(setup)
    with pytest.raises(ValueError):
        await gitlab_cli.resolve_absent(item.run_id)
    await due(sessions, item, expired=True)
    client = AsyncMock(spec=GitLabClient)
    client.reconcile.return_value = None, 1
    await poll_once(sessions, config, client)
    await gitlab_cli.resolve_absent(item.run_id)
    async with sessions() as session:
        row = await session.get(GitLabPipeline, item.run_id)
        assert row.pipeline_status == "not_created"
        assert row.error_code == "operator_confirmed_not_created"
        await create_run(session, snapshot.id, "safe next launch", {})
    with pytest.raises(ValueError):
        await gitlab_cli.resolve_absent(item.run_id)


@pytest.mark.parametrize("poll_error", [False, True])
async def test_known_pipeline_deadline_is_incomplete_preserves_observations_and_rejects_compare(isolated_database, monkeypatch, poll_error):
    sessions = isolated_database
    async with sessions() as session:
        service = await create_service(session, "deadline-api", "Deadline API", is_public=True)
        snapshot = await import_sdl(session, service.id, "type Query { health: String }")
    config = GitLabSettings("https://gitlab.example", "test-token", {service.id: 123}, "https://coverage.example")
    monkeypatch.setitem(app.dependency_overrides, get_session_factory, lambda: sessions)
    # This test owns real transactions: SET TRANSACTION must precede all reads.
    session = sessions()
    setup = session, sessions, service, snapshot, config, set()
    monkeypatch.setenv("COVERAGE_API_KEY", "sol-audit-test-key-" + "x" * 40)
    baseline = await create_run(session, snapshot.id, "Complete baseline", {})
    await finish_run(session, baseline.id)
    item = await prepare(setup)
    observation = await record_observation(session, RequestObservationInput(
        run_id=item.run_id, analysis=AnalysisResult(status="success", operation_type="mutation",
        fields=(FieldReference("Query", "health"),)), http_method="POST", http_status=200,
        response_has_data=True, graphql_has_errors=False,
    ))
    await session.close()
    client = AsyncMock(spec=GitLabClient)
    client.launch.return_value = info("running")
    await launch(sessions, config, item, client)
    await due(sessions, item, expired=True)
    client.pipeline.return_value = info("running")
    if poll_error:
        client.pipeline.side_effect = GitLabError("gitlab_network_error")
    assert await poll_once(sessions, config, client)
    async with sessions() as s:
        run = await s.get(Run, item.run_id)
        finished = run.finished_at
        assert run.status == "failed"
        row = await s.get(GitLabPipeline, item.run_id)
        assert row.collection_incomplete and row.finished_at is None
        assert (await get_run_graph(s, item.run_id)).summary.covered_fields == 1
        from app.models import ObservedRequest, RequestFieldUsage
        assert await s.get(ObservedRequest, observation) is not None
        assert await s.scalar(select(func.count()).select_from(RequestFieldUsage)
                              .where(RequestFieldUsage.request_id == observation)) == 1
        with pytest.raises(ConflictError):
            await prepare_launch(s, config, service.id, snapshot.id, "main", "duplicate")
    async def check_comparison():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="https://graphql.rebyte.fun",
                                    headers={"X-Coverage-Api-Key": "sol-audit-test-key-" + "x" * 40}) as http:
            result = await http.post("/api/runs/compare", json={
                "runIds": [str(baseline.id), str(item.run_id)], "baselineRunId": str(baseline.id)})
            assert result.status_code == 409, result.text
            assert result.json()["code"] == "RUN_NOT_COMPLETED"
    await check_comparison()
    # Restarted worker continues checking the existing ID, never POSTing again.
    await due(sessions, item)
    client.pipeline.side_effect = None
    client.pipeline.return_value = info("success")
    assert await poll_once(sessions, config, client)
    assert client.launch.await_count == 1 and client.pipeline.await_count == 2
    client.reconcile.assert_not_called()
    async with sessions() as s:
        run, row = await s.get(Run, item.run_id), await s.get(GitLabPipeline, item.run_id)
        assert run.status == "failed" and run.finished_at == finished
        assert row.pipeline_status == "success" and row.finished_at is not None
        assert row.collection_incomplete and row.error_code == "tracking_deadline_exceeded"
        result = gitlab_api.output(row, run)
        assert result.collection_incomplete and result.tracking_finished
        await create_run(s, snapshot.id, "safe next", {})
    await check_comparison()


async def test_first_terminal_observed_after_deadline_is_conservatively_incomplete(setup):
    _, sessions, _, _, config, _ = setup
    item = await prepare(setup)
    await due(sessions, item, expired=True)
    claimed = await claim(sessions)
    await apply_result(sessions, claimed, config, info=info("success"))
    async with sessions() as s:
        assert (await s.get(Run, item.run_id)).status == "failed"
        assert (await s.get(GitLabPipeline, item.run_id)).collection_incomplete


@pytest.mark.parametrize('expired', [False, True])
async def test_monitor_defers_terminal_finish_while_graphql_is_admitted(setup, expired):
    from app.test_executions import admit_request, release_request
    _, sessions, _, _, config, _ = setup
    item = await prepare(setup)
    async with sessions() as session:
        await admit_request(session, item.run_id)
    if expired:
        await due(sessions, item, expired=True)
    await apply_result(sessions, item, config, info=info('success'))
    async with sessions() as session:
        run = await session.get(Run, item.run_id)
        link = await session.get(GitLabPipeline, item.run_id)
        assert run.status == 'running' and run.in_flight_requests == 1
        assert link.finished_at is None and link.pipeline_status == 'success'
        assert link.lease_token is None
    await release_request(sessions, run_id=item.run_id)
    await due(sessions, item)
    client = AsyncMock(spec=GitLabClient)
    client.pipeline.return_value = info('success')
    assert await poll_once(sessions, config, client)
    async with sessions() as session:
        run = await session.get(Run, item.run_id)
        assert run.status == ('failed' if expired else 'completed')
        assert (await session.get(GitLabPipeline, item.run_id)).finished_at is not None
    client.launch.assert_not_called()
