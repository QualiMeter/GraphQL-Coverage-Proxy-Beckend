"""Refresh stored GitLab job pages independently of coverage collection."""
import asyncio
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.diagnostics import internal_failure
from app.gitlab_client import GitLabClient, GitLabError
from app.gitlab_config import GitLabSettings
from app.gitlab_runs import now
from app.models import GitLabJob, GitLabPipeline, SchemaSnapshot, TestRun


@dataclass(frozen=True)
class JobSync:
    run_id: uuid.UUID
    service_id: uuid.UUID
    host: str
    project_id: int
    pipeline_id: int
    branch: str
    page: int
    lease: uuid.UUID
    needs_sha: bool


async def claim_job_sync(sessions) -> JobSync | None:
    stamp = now()
    async with sessions.begin() as session:
        result = (await session.execute(
            select(GitLabPipeline, SchemaSnapshot.service_id)
            .join(TestRun, TestRun.id == GitLabPipeline.run_id)
            .join(SchemaSnapshot, SchemaSnapshot.id == TestRun.snapshot_id)
            .where(GitLabPipeline.jobs_next_check_at <= stamp, GitLabPipeline.pipeline_id.is_not(None))
            .order_by(GitLabPipeline.jobs_next_check_at, GitLabPipeline.run_id)
            .with_for_update(of=GitLabPipeline, skip_locked=True).limit(1)
        )).first()
        if result is None:
            return None
        row, service_id = result
        lease = uuid.uuid4()
        row.jobs_lease_token = lease
        # At most two bounded HTTP requests. This also lets another worker
        # recover the current page if this process exits mid-request.
        row.jobs_next_check_at = stamp + timedelta(seconds=60)
        return JobSync(row.run_id, service_id, row.gitlab_host, row.project_id,
                       row.pipeline_id, row.ref, row.jobs_page, lease, row.commit_sha is None)


async def sync_jobs_once(sessions, settings: GitLabSettings, client: GitLabClient) -> bool:
    item = await claim_job_sync(sessions)
    if item is None:
        return False

    sha = None
    try:
        if item.host != settings.host or settings.projects.get(item.service_id) != item.project_id:
            raise GitLabError("gitlab_configuration_changed")
        if item.needs_sha:
            pipeline = await client.pipeline(item.project_id, item.pipeline_id)
            if pipeline.ref is not None and pipeline.ref != item.branch:
                raise GitLabError("gitlab_invalid_response")
            sha = pipeline.commit_sha
        jobs, has_more = await client.jobs(item.project_id, item.pipeline_id, item.page)
    except GitLabError:
        jobs = None  # Preserve any previously recorded pages and retry.

    async with sessions.begin() as session:
        row = await session.get(GitLabPipeline, item.run_id, with_for_update=True)
        if row is None or row.jobs_lease_token != item.lease or row.pipeline_id != item.pipeline_id:
            return True
        stamp = now()
        row.jobs_lease_token = None
        if row.commit_sha is None and sha is not None:
            row.commit_sha = sha
        if jobs is None:
            row.jobs_next_check_at = stamp + timedelta(seconds=max(60, settings.poll_seconds))
            return True
        if jobs:
            statement = insert(GitLabJob).values([{"run_id": item.run_id, **asdict(job)} for job in jobs])
            await session.execute(statement.on_conflict_do_update(
                index_elements=[GitLabJob.run_id, GitLabJob.job_id],
                set_={column: statement.excluded[column] for column in (
                    "name", "stage", "status", "url", "created_at", "started_at", "finished_at",
                )},
            ))
        if has_more:
            row.jobs_page += 1
            row.jobs_next_check_at = stamp
        else:
            row.jobs_page = 1
            row.jobs_synced_at = stamp
            row.jobs_next_check_at = (
                stamp + timedelta(seconds=max(30, settings.poll_seconds)) if row.finished_at is None else None
            )
    return True


async def monitor_jobs(sessions, settings: GitLabSettings):
    client = GitLabClient(settings)
    while True:
        try:
            for _ in range(10):
                if not await sync_jobs_once(sessions, settings, client):
                    break
        except Exception as exc:
            internal_failure("gitlab_jobs_monitor", exc)
        await asyncio.sleep(settings.poll_seconds)
