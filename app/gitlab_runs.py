"""Durable launch intents and leased polling; database transactions never span HTTP."""
import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.diagnostics import internal_failure
from app.gitlab_client import GitLabClient, GitLabError, PipelineInfo
from app.gitlab_config import GitLabSettings
from app.models import GitLabPipeline, SchemaSnapshot, TestRun
from app.services import NotFoundError, create_run, finish_run

TERMINAL = {"success", "failed", "canceled", "skipped"}
LEASE_SECONDS = 90


def now():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Work:
    run_id: uuid.UUID
    host: str
    project: int
    ref: str
    pipeline_id: int | None
    created_at: datetime
    page: int
    lease: uuid.UUID


def work(row: GitLabPipeline) -> Work:
    return Work(row.run_id, row.gitlab_host, row.project_id, row.ref,
                row.pipeline_id, row.created_at, row.reconcile_page, row.lease_token)


async def prepare_launch(session, settings: GitLabSettings, service_id, snapshot_id, ref, name) -> Work:
    snapshot = await session.get(SchemaSnapshot, snapshot_id)
    if snapshot is None or snapshot.service_id != service_id:
        raise NotFoundError("Schema snapshot not found for this service")
    project = settings.project(service_id)
    # Both the run and intent are committed together, before the one POST attempt.
    run = await create_run(session, snapshot_id, name or f"GitLab E2E: {ref}"[:255], {}, commit=False)
    stamp = now()
    row = GitLabPipeline(
        run_id=run.id, gitlab_host=settings.host, project_id=project, ref=ref,
        pipeline_status="creating", created_at=stamp,
        deadline_at=stamp + timedelta(seconds=settings.max_run_seconds), next_check_at=stamp,
        lease_token=uuid.uuid4(), lease_until=stamp + timedelta(seconds=LEASE_SECONDS),
        reconcile_page=1, check_failures=0,
    )
    session.add(row)
    await session.commit()
    return work(row)


async def claim(sessions) -> Work | None:
    stamp = now()
    async with sessions() as session:
        row = await session.scalar(select(GitLabPipeline).where(
            GitLabPipeline.finished_at.is_(None), GitLabPipeline.next_check_at <= stamp,
            or_(GitLabPipeline.lease_until.is_(None), GitLabPipeline.lease_until <= stamp),
        ).order_by(GitLabPipeline.next_check_at, GitLabPipeline.run_id).with_for_update(skip_locked=True).limit(1))
        if row is None:
            return None
        row.lease_token = uuid.uuid4()
        row.lease_until = stamp + timedelta(seconds=LEASE_SECONDS)
        result = work(row)
        await session.commit()
        return result


async def apply_result(sessions, item: Work, settings: GitLabSettings, *, info: PipelineInfo | None = None,
                       error: GitLabError | None = None, rejected: bool = False, page: int | None = None):
    async with sessions() as session:
        # All finish paths acquire run then pipeline: no lock-order inversion.
        run = await session.scalar(select(TestRun).where(TestRun.id == item.run_id).with_for_update()
                                   .execution_options(populate_existing=True))
        row = await session.scalar(select(GitLabPipeline).where(GitLabPipeline.run_id == item.run_id)
                                   .with_for_update().execution_options(populate_existing=True))
        if row is None or row.finished_at is not None or row.lease_token != item.lease:
            return
        stamp = now()
        # Once collection expires it cannot become complete again, even when
        # a later poll finally learns the terminal pipeline outcome.
        if not rejected and stamp >= row.deadline_at:
            row.collection_incomplete = True
        if page is not None:
            row.reconcile_page = page
        if info is None and row.pipeline_id is None and not rejected:
            row.pipeline_status = "launch_unknown"
        if info is not None:
            row.pipeline_id, row.pipeline_url, row.pipeline_status = info.id, info.url, info.status
            if not row.collection_incomplete:
                row.error_code = None
            row.check_failures = 0
        elif error is not None:
            if not row.collection_incomplete:
                row.error_code = error.code
            row.check_failures += 1
            if row.pipeline_id is None:
                row.pipeline_status = "launch_rejected" if rejected else "launch_unknown"
        if row.collection_incomplete:
            row.error_code = "tracking_deadline_exceeded"
        if rejected or (info is not None and info.status in TERMINAL):
            if run.status == "running":
                await finish_run(session, item.run_id, pipeline_managed=True,
                                 status="failed" if rejected or row.collection_incomplete else "completed", commit=False)
            row.finished_at = stamp
        elif stamp >= row.deadline_at:
            # Stop collection on a finite deadline, retain observations, and keep
            # reconciling GitLab. The unfinished link quarantines this snapshot
            # against duplicate launches until GitLab's outcome is known.
            if run.status == "running":
                await finish_run(session, item.run_id, pipeline_managed=True,
                                 status="failed", commit=False)
            row.error_code = "tracking_deadline_exceeded"
        delay = min(300, settings.poll_seconds * 2 ** min(row.check_failures, 5))
        row.next_check_at = stamp + timedelta(seconds=delay)
        row.lease_token = None
        row.lease_until = None
        await session.commit()


async def launch(sessions, settings: GitLabSettings, item: Work, client: GitLabClient):
    try:
        info = await client.launch(item.project, item.ref, item.run_id)
    except GitLabError as exc:
        await apply_result(sessions, item, settings, error=exc, rejected=not exc.ambiguous)
        return exc
    # A failed commit leaves a durable intent. The worker reconciles by UUID;
    # it must never repeat POST, including after process death or lost DB ACK.
    await apply_result(sessions, item, settings, info=info)
    return None


async def poll_once(sessions, settings: GitLabSettings, client: GitLabClient) -> bool:
    item = await claim(sessions)
    if item is None:
        return False
    try:
        if item.host != settings.host:
            # Persisted hosts never select a destination or receive current keys.
            raise GitLabError("gitlab_configuration_changed")
        async with asyncio.timeout(60):
            if item.pipeline_id is None:
                info, page = await client.reconcile(item.project, item.ref, item.run_id, item.created_at, item.page)
            else:
                info, page = await client.pipeline(item.project, item.pipeline_id), None
        await apply_result(sessions, item, settings, info=info, page=page)
    except (GitLabError, TimeoutError) as exc:
        error = exc if isinstance(exc, GitLabError) else GitLabError("gitlab_network_error")
        await apply_result(sessions, item, settings, error=error)
    return True


async def monitor(sessions, settings: GitLabSettings):
    client = GitLabClient(settings)
    while True:
        try:
            # Bounded batches and leases allow multiple Uvicorn processes.
            for _ in range(20):
                if not await poll_once(sessions, settings, client):
                    break
        except Exception as exc:
            internal_failure("gitlab_monitor", exc)
        await asyncio.sleep(settings.poll_seconds)
