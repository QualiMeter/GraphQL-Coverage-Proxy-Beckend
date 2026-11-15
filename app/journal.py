"""SQL pagination and snapshot-scoped journal reads. No bodies are reconstructed."""

import re
import uuid
from datetime import timezone
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.api_models import ApiModel, RequestDetailOut, RequestOut, SelectedFieldOut
from app.contracts import AnalysisStatus, OperationType
from app.models import ObservedRequest, RequestFieldUsage, SchemaField, SchemaSnapshot, SchemaType, TestRun
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


class RunFilters(DateRange):
    snapshot_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    status: Literal["running", "completed"] | None = None
    limit: int | None = Field(default=None, ge=1, le=500, description="Omit for all matching history (legacy compatibility); use explicit pages for large histories.")
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def exactly_one_scope(self):
        if (self.snapshot_id is None) == (self.service_id is None):
            raise ValueError("Provide exactly one of snapshotId or serviceId")
        return self


class RequestFilters(DateRange):
    operation_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="Exact lowercase SHA-256 identity returned by /operations.")
    operation_name: str | None = Field(default=None, max_length=255, description="Exact case-sensitive name; omission includes unnamed operations.")
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


async def list_run_page(session: AsyncSession, filters: RunFilters, *, include_metadata=False):
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
    total = await session.scalar(select(func.count()).select_from(TestRun).where(*conditions))
    rows = (await session.scalars(
        select(TestRun).where(*conditions).order_by(TestRun.started_at.desc(), TestRun.id.desc())
        .limit(filters.limit).offset(filters.offset)
    )).all()
    return [run_out(row, include_metadata=include_metadata) for row in rows], total


def request_out(row: ObservedRequest) -> RequestOut:
    return RequestOut(
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
    for name in ("operation_key", "operation_name", "operation_type", "analysis_status", "graphql_has_errors", "http_status"):
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
        safe_graphql_document=row.safe_graphql_document, document_availability=row.document_availability,
        counted_in_coverage=row.analysis_status == "success" and row.response_has_data is True,
        selected_fields=[SelectedFieldOut(type_name=t, field_name=f, return_type=r) for t, f, r in fields])
