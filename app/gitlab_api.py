import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.access import require_role
from app.api_models import ApiModel
from app.auth import Principal, current_principal, visibility_kwargs
from app.contracts import RunStatus
from app.database import get_session_factory
from app.diagnostics import internal_failure
from app.gitlab_client import GitLabClient, GitLabError
from app.gitlab_config import GitLabConfigurationError, gitlab_settings
from app.gitlab_runs import apply_result, launch, prepare_launch
from app.models import GitLabJob, GitLabPipeline, TestRun
from app.services import ConflictError, NotFoundError, ensure_visible_run, ensure_visible_service

router = APIRouter(prefix="/api", tags=["GitLab E2E"], responses={
    401: {"description": "Authentication required"},
    403: {"description": "Insufficient role or invalid Origin/CSRF"},
    404: {"description": "Resource not found or access denied"},
    409: {"description": "An active run or unreconciled pipeline already exists"},
    502: {"description": "GitLab rejected the launch"},
    503: {"description": "GitLab configuration or storage unavailable"},
})


async def gitlab_principal(request: Request, sessions=Depends(get_session_factory)):
    async with sessions() as session:
        return await current_principal(request, session)


operator = require_role("operator", principal_dependency=gitlab_principal)
viewer = require_role("viewer", principal_dependency=gitlab_principal)


class LaunchInput(ApiModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")
    service_id: uuid.UUID
    snapshot_id: uuid.UUID
    ref: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("ref")
    @classmethod
    def valid_ref(cls, value):
        if value != value.strip() or value in {".", ".."} or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid branch name")
        return value


class PipelineOut(ApiModel):
    run_id: uuid.UUID
    pipeline_id: int | None
    pipeline_url: str | None
    pipeline_status: str
    run_status: RunStatus
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None
    collection_incomplete: bool = Field(description="Collection stopped before a trustworthy terminal outcome; observations are retained and this flag never clears.")
    tracking_finished: bool
    project_id: int | None = None
    branch: str | None = None
    commit_sha: str | None = None
    environment: str | None = None


class JobOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: int
    name: str
    stage: str
    status: str
    url: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobPageOut(ApiModel):
    run_id: uuid.UUID
    pipeline_id: int | None
    jobs_synced_at: datetime | None
    jobs_finalized: bool
    total: int
    jobs: list[JobOut]


def output(row, run):
    confirmed = row.pipeline_id is not None
    return PipelineOut(run_id=row.run_id, pipeline_id=row.pipeline_id, pipeline_url=row.pipeline_url,
                       pipeline_status=row.pipeline_status, run_status=run.status,
                       error_code=row.error_code, created_at=row.created_at, finished_at=row.finished_at,
                       collection_incomplete=row.collection_incomplete,
                       tracking_finished=row.finished_at is not None,
                       project_id=row.project_id if confirmed else None, branch=row.ref if confirmed else None,
                       commit_sha=row.commit_sha if confirmed else None, environment=row.environment if confirmed else None)


def settings():
    try:
        return gitlab_settings()
    except GitLabConfigurationError:
        raise HTTPException(503, "GitLab integration is not configured for this service") from None


def gitlab_error(exc: GitLabError):
    return HTTPException(502, {"code": exc.code})


async def read_pipeline(sessions, run_id):
    async with sessions() as session:
        row = await session.get(GitLabPipeline, run_id)
        run = await session.get(TestRun, run_id)
        if row is None or run is None:
            raise NotFoundError("GitLab pipeline not found")
        return output(row, run)


@router.post("/runs/gitlab", status_code=202, response_model=PipelineOut,
             description="Creates a coverage run and attempts GitLab POST once. A launch_unknown response "
                         "is reconciled in the background; never repeat the launch automatically.")
async def post_gitlab_run(payload: LaunchInput, sessions=Depends(get_session_factory),
                          principal: Principal = Depends(operator)):
    try:
        config = settings()
        async with sessions() as session:
            await ensure_visible_service(session, payload.service_id, require_grant=True,
                                         **visibility_kwargs(principal))
            item = await prepare_launch(session, config, payload.service_id, payload.snapshot_id,
                                        payload.ref, payload.name)
        client = GitLabClient(config)
        # Restrict ref to an existing branch, rather than a same-named tag.
        # A failed preflight is definitive: no POST has been attempted yet.
        try:
            await client.check_branch(item.project, payload.ref)
        except GitLabError as exc:
            await apply_result(sessions, item, config, error=exc, rejected=True)
            raise HTTPException(502, {"code": exc.code, "runId": str(item.run_id)}) from None
        error = await launch(sessions, config, item, client)
        result = await read_pipeline(sessions, item.run_id)
        if error is not None and not error.ambiguous:
            raise HTTPException(502, {"code": error.code, "runId": str(item.run_id)})
        return result
    except GitLabConfigurationError:
        raise HTTPException(503, "GitLab integration is not configured for this service") from None
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from None
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except GitLabError as exc:
        raise gitlab_error(exc) from None
    except SQLAlchemyError as exc:
        internal_failure("gitlab_launch_storage", exc)
        raise HTTPException(503, "GitLab launch storage unavailable; check run history before retrying") from None


@router.get("/runs/{run_id}/gitlab", response_model=PipelineOut)
async def get_gitlab_run(run_id: uuid.UUID, sessions=Depends(get_session_factory),
                         principal: Principal = Depends(viewer)):
    try:
        async with sessions() as session:
            await ensure_visible_run(session, run_id, require_grant=True, **visibility_kwargs(principal))
        return await read_pipeline(sessions, run_id)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from None
    except SQLAlchemyError as exc:
        internal_failure("gitlab_status_storage", exc, run_id=run_id)
        raise HTTPException(503, "GitLab status storage unavailable") from None


@router.get("/services/{service_id}/gitlab/branches", response_model=list[str])
async def get_gitlab_branches(service_id: uuid.UUID, page: int = Query(default=1, ge=1, le=10000),
                              sessions=Depends(get_session_factory), principal: Principal = Depends(operator)):
    try:
        async with sessions() as session:
            await ensure_visible_service(session, service_id, require_grant=True, **visibility_kwargs(principal))
        config = settings()
        return await GitLabClient(config).branches(config.project(service_id), page)
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from None
    except GitLabConfigurationError:
        raise HTTPException(503, "GitLab integration is not configured for this service") from None
    except GitLabError as exc:
        raise gitlab_error(exc) from None
    except SQLAlchemyError as exc:
        internal_failure("gitlab_branches_storage", exc, service_id=service_id)
        raise HTTPException(503, "GitLab service storage unavailable") from None


@router.get("/runs/{run_id}/gitlab/jobs", response_model=JobPageOut,
            description="Stored GitLab jobs, newest job ID first. Requires a service grant or admin access.")
async def get_gitlab_jobs(
    run_id: uuid.UUID, limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0),
    sessions=Depends(get_session_factory), principal: Principal = Depends(viewer),
):
    try:
        async with sessions() as session:
            await ensure_visible_run(session, run_id, require_grant=True, **visibility_kwargs(principal))
            pipeline = await session.get(GitLabPipeline, run_id)
            if pipeline is None:
                raise NotFoundError("GitLab pipeline not found")
            scope = GitLabJob.run_id == run_id
            total = await session.scalar(select(func.count()).select_from(GitLabJob).where(scope))
            jobs = (await session.scalars(select(GitLabJob).where(scope)
                    .order_by(GitLabJob.job_id.desc()).limit(limit).offset(offset))).all()
            return JobPageOut(
                run_id=run_id, pipeline_id=pipeline.pipeline_id, jobs_synced_at=pipeline.jobs_synced_at,
                jobs_finalized=pipeline.jobs_synced_at is not None and pipeline.jobs_next_check_at is None,
                total=total, jobs=[JobOut.model_validate(job) for job in jobs],
            )
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from None
    except SQLAlchemyError as exc:
        internal_failure("gitlab_jobs_storage", exc, run_id=run_id)
        raise HTTPException(503, "GitLab jobs storage unavailable") from None
