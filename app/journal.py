"""SQL pagination and snapshot-scoped journal reads. No bodies are reconstructed."""

import re
import uuid
from datetime import timezone
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.api_models import ApiModel, RequestDetailOut, RequestOut, RunCiOut, SelectedFieldOut
from app.contracts import AnalysisStatus, OperationType, RunStatus
from app.gitlab_client import COMMIT_SHA_PATTERN
from app.models import GitLabJob, GitLabPipeline, ObservedRequest, RequestFieldUsage, SchemaField, SchemaSnapshot, SchemaType, TestRun
from app.services import NotFoundError, run_out


class DateRange(ApiModel):
    from_: AwareDatetime | None = Field(default=None, alias="from", description="Inclusive ISO-8601 timestamp with timezone; normalized to UTC.")
    to: AwareDatetime | None = Field(default=None, description="Exclusive ISO-8601 timestamp with timezone; normalized to UTC.")

    @field_validator("from_", "to", mode="before")
    @classmethod
    def iso_timestamp(cls, value):
        if isinstance(value, str) and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
        ):
            raise ValueError("Use an ISO-8601 timestamp with timezone and seconds")
        return value

    @field_validator("from_", "to")
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.from_ is not None and self.to is not None and self.from_ >= self.to:
            raise ValueError("from must be earlier than to")
        return self


RunSort = Literal[
    "startedAt", "branch", "commitSha", "environment", "pipelineId", "pipelineStatus", "jobId", "jobName",
]
PIPELINE_SORT = {
    "branch": GitLabPipeline.ref, "commitSha": GitLabPipeline.commit_sha,
    "environment": GitLabPipeline.environment, "pipelineId": GitLabPipeline.pipeline_id,
    "pipelineStatus": GitLabPipeline.pipeline_status,
}
JOB_SORT = {"jobId": GitLabJob.job_id, "jobName": GitLabJob.name}
PIPELINE_FILTERS = {
    "branch": GitLabPipeline.ref, "commit_sha": GitLabPipeline.commit_sha,
    "environment": GitLabPipeline.environment, "pipeline_id": GitLabPipeline.pipeline_id,
    "pipeline_status": GitLabPipeline.pipeline_status,
}
JOB_FILTERS = {"job_id": GitLabJob.job_id, "job_name": GitLabJob.name, "job_status": GitLabJob.status}


class RunFilters(DateRange):
    snapshot_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    status: RunStatus | None = None
    branch: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$")
    commit_sha: str | None = Field(default=None, pattern=COMMIT_SHA_PATTERN)
    environment: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")
    pipeline_id: int | None = Field(default=None, gt=0, lt=2**63)
    pipeline_status: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    job_id: int | None = Field(default=None, gt=0, lt=2**63)
    job_name: str | None = Field(default=None, min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f]+$")
    job_status: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    sort_by: RunSort = "startedAt"
    sort_order: Literal["asc", "desc"] = "desc"
    limit: int | None = Field(default=None, ge=1, le=500, description="Omit for all matching history (legacy compatibility); use explicit pages for large histories.")
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def exactly_one_scope(self):
        if (self.snapshot_id is None) == (self.service_id is None):
            raise ValueError("Provide exactly one of snapshotId or serviceId")
        return self

    @property
    def uses_ci(self) -> bool:
        return self.sort_by != "startedAt" or any(
            getattr(self, field) is not None for field in (*PIPELINE_FILTERS, *JOB_FILTERS)
        )


class RequestFilters(DateRange):
    test_execution_id: uuid.UUID | None = None
    operation_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="Exact lowercase SHA-256 identity returned by /operations.")
    operation_name: str | None = Field(default=None, description="Exact full case-sensitive name; omission includes unnamed operations.")
    operation_type: OperationType | None = None
    type_name: str | None = Field(default=None, min_length=1, max_length=255)
    field_name: str | None = Field(default=None, min_length=1, max_length=255)
    analysis_status: AnalysisStatus | None = None
    graphql_has_errors: bool | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    http_status_class: int | None = Field(default=None, ge=1, le=5)
    sort_by: Literal["createdAt", "durationMs"] = Field(default="createdAt", description="Duration NULLs sort last in either direction.")
    sort_order: Literal["asc", "desc"] = Field(default="desc", description="id tie-breaker uses the same direction.")
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def field_requires_type(self):
        if self.field_name is not None and self.type_name is None:
            raise ValueError("fieldName requires typeName")
        return self


def _dates(conditions, column, filters):
    if filters.from_ is not None:
        conditions.append(column >= filters.from_)
    if filters.to is not None:
        conditions.append(column < filters.to)


def ci_out(row: GitLabPipeline) -> RunCiOut:
    return RunCiOut(
        project_id=row.project_id, branch=row.ref, commit_sha=row.commit_sha, environment=row.environment,
        pipeline_id=row.pipeline_id, pipeline_url=row.pipeline_url, pipeline_status=row.pipeline_status,
        jobs_synced_at=row.jobs_synced_at,
        jobs_finalized=row.jobs_synced_at is not None and row.jobs_next_check_at is None,
    )


async def list_run_page(session: AsyncSession, filters: RunFilters, *, include_metadata=False, include_ci=False):
    if filters.uses_ci and not include_ci:
        raise ValueError("CI filtering and sorting require CI access")
    conditions = []
    if filters.snapshot_id is not None:
        conditions.append(TestRun.snapshot_id == filters.snapshot_id)
    else:
        conditions.append(TestRun.snapshot_id.in_(
            select(SchemaSnapshot.id).where(SchemaSnapshot.service_id == filters.service_id)
        ))
    if filters.status is not None:
        conditions.append(TestRun.status == filters.status)
    _dates(conditions, TestRun.started_at, filters)
    total_query = select(func.count()).select_from(TestRun)
    rows_query = select(TestRun)
    sort_column = TestRun.started_at
    if include_ci:
        confirmed = (GitLabPipeline.run_id == TestRun.id) & GitLabPipeline.pipeline_id.is_not(None)
        rows_query = rows_query.add_columns(GitLabPipeline).outerjoin(GitLabPipeline, confirmed)
        if any(getattr(filters, field) is not None for field in PIPELINE_FILTERS):
            total_query = total_query.join(GitLabPipeline, confirmed)
            conditions.extend(column == getattr(filters, field) for field, column in PIPELINE_FILTERS.items()
                              if getattr(filters, field) is not None)
        job_conditions = [column == getattr(filters, field) for field, column in JOB_FILTERS.items()
                          if getattr(filters, field) is not None]
        jobs = select(GitLabJob.job_id).where(GitLabJob.run_id == TestRun.id, *job_conditions)
        if job_conditions:
            conditions.append(jobs.exists())
        if filters.sort_by in JOB_SORT:
            # Pick a deterministic key from matching jobs without duplicating runs.
            sort_column = jobs.with_only_columns(func.min(JOB_SORT[filters.sort_by])).scalar_subquery()
        else:
            sort_column = PIPELINE_SORT.get(filters.sort_by, TestRun.started_at)
    direction = "asc" if filters.sort_order == "asc" else "desc"
    order = getattr(sort_column, direction)().nulls_last()
    tie = getattr(TestRun.id, direction)()

    total = await session.scalar(total_query.where(*conditions))
    rows = (await session.execute(
        rows_query.where(*conditions).order_by(order, tie).limit(filters.limit).offset(filters.offset)
    )).all()
    output = []
    for row, *pipeline in rows:
        item = run_out(row, include_metadata=include_metadata)
        if pipeline and pipeline[0] is not None:
            item.ci = ci_out(pipeline[0])
        output.append(item)
    return output, total


def request_out(row: ObservedRequest) -> RequestOut:
    return RequestOut(
        test_execution_id=getattr(row, "test_execution_id", None),
        id=row.id, run_id=row.run_id, operation_key=row.operation_key,
        operation_name=row.operation_name, operation_type=row.operation_type,
        http_method=row.http_method, http_status=row.http_status,
        analysis_status=row.analysis_status, response_has_data=row.response_has_data,
        graphql_has_errors=row.graphql_has_errors, duration_ms=row.duration_ms,
        error_code=row.error_code, created_at=row.created_at.astimezone(timezone.utc),
    )


async def list_request_page(session: AsyncSession, run_id: uuid.UUID, filters: RequestFilters):
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFoundError("Test run not found")
    conditions = [ObservedRequest.run_id == run_id]
    for name in ("test_execution_id", "operation_key", "operation_name", "operation_type", "analysis_status", "graphql_has_errors", "http_status"):
        value = getattr(filters, name)
        if value is not None:
            conditions.append(getattr(ObservedRequest, name) == value)
    if filters.http_status_class is not None:
        lower = filters.http_status_class * 100
        conditions.append(ObservedRequest.http_status.between(lower, lower + 99))
    _dates(conditions, ObservedRequest.created_at, filters)
    if filters.type_name is not None:
        field_conditions = [
            RequestFieldUsage.request_id == ObservedRequest.id,
            SchemaType.snapshot_id == run.snapshot_id, SchemaType.name == filters.type_name,
        ]
        if filters.field_name is not None:
            field_conditions.append(SchemaField.name == filters.field_name)
        conditions.append(exists(select(1).select_from(RequestFieldUsage)
            .join(SchemaField, SchemaField.id == RequestFieldUsage.field_id)
            .join(SchemaType, SchemaType.id == SchemaField.type_id).where(*field_conditions)))
    total = await session.scalar(select(func.count()).select_from(ObservedRequest).where(*conditions))
    column = ObservedRequest.created_at if filters.sort_by == "createdAt" else ObservedRequest.duration_ms
    order = column.asc() if filters.sort_order == "asc" else column.desc()
    tie = ObservedRequest.id.asc() if filters.sort_order == "asc" else ObservedRequest.id.desc()
    rows = (await session.scalars(select(ObservedRequest).options(
        defer(ObservedRequest.safe_graphql_document), defer(ObservedRequest.document_availability),
        defer(ObservedRequest.graphql_error_details), defer(ObservedRequest.graphql_error_count),
        defer(ObservedRequest.graphql_error_details_availability),
    ).where(*conditions)
        .order_by(order.nulls_last(), tie).limit(filters.limit).offset(filters.offset))).all()
    return [request_out(row) for row in rows], total


async def get_request_detail(session: AsyncSession, run_id: uuid.UUID, request_id: uuid.UUID):
    # Match both IDs before selecting fields; never disclose another run's request.
    row = await session.scalar(select(ObservedRequest).where(
        ObservedRequest.id == request_id, ObservedRequest.run_id == run_id,
    ))
    if row is None:
        raise NotFoundError("Request not found")
    fields = (await session.execute(select(SchemaType.name, SchemaField.name, SchemaField.return_type)
        .select_from(RequestFieldUsage)
        .join(SchemaField, SchemaField.id == RequestFieldUsage.field_id)
        .join(SchemaType, SchemaType.id == SchemaField.type_id)
        .join(TestRun, TestRun.snapshot_id == SchemaType.snapshot_id)
        .where(RequestFieldUsage.request_id == request_id, TestRun.id == run_id)
        .distinct().order_by(SchemaType.name, SchemaField.name, SchemaField.return_type))).all()
    return RequestDetailOut(**request_out(row).model_dump(),
        field_evidence_version=row.field_evidence_version,
        safe_graphql_document=row.safe_graphql_document, document_availability=row.document_availability,
        graphql_error_count=row.graphql_error_count,
        graphql_error_details_availability=row.graphql_error_details_availability,
        graphql_error_details=row.graphql_error_details,
        counted_in_coverage=row.analysis_status == "success" and row.response_has_data is True,
        selected_fields=[SelectedFieldOut(type_name=t, field_name=f, return_type=r) for t, f, r in fields])
