"""Snapshot-scoped evidence aggregation, deliberately separate from Coverage Engine."""
import uuid

from pydantic import Field
from sqlalchemy import and_, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_models import ApiModel
from app.contracts import EvidenceState
from app.models import ObservedRequest, RequestFieldUsage, RequestFieldEvidence, SchemaField, SchemaType, TestRun
from app.services import NotFoundError, counted_observation


class EvidenceFilters(ApiModel):
    type_name: str | None = Field(default=None, min_length=1, max_length=255)
    field_name: str | None = Field(default=None, min_length=1, max_length=255)
    state: EvidenceState | None = Field(default=None, description="Fields with at least one request in this state.")
    request_id: uuid.UUID | None = Field(default=None, description="Restrict to one request belonging to this run.")
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class FieldEvidenceOut(ApiModel):
    field_id: uuid.UUID
    type_name: str
    field_name: str
    selected_request_count: int = Field(description="All successfully analyzed requests selecting this coordinate, including those without data.")
    selected_hits: int = Field(description="Existing Coverage Engine hits: successful analysis and response_has_data=true.")
    observed_hits: int = Field(description="Requests with proven presence; once per coordinate/request, not resolver successes or array element count.")
    not_observed_count: int
    indeterminate_count: int
    unavailable_count: int


class FieldEvidencePage(ApiModel):
    run_id: uuid.UUID
    snapshot_id: uuid.UUID
    request_count: int
    selection_unavailable_request_count: int = Field(description="Requests whose analysis failed; their selected coordinates cannot be attributed to individual fields.")
    evidence_unavailable_request_count: int = Field(description="Requests without a complete plan/evidence version or with at least one unavailable field. Includes historical requests.")
    total: int
    limit: int
    offset: int
    fields: list[FieldEvidenceOut]


async def get_field_evidence(session: AsyncSession, run_id: uuid.UUID, filters: EvidenceFilters) -> FieldEvidencePage:
    # Caller checks journal ACL before any aggregates. Every SQL path is scoped
    # to both the run and its immutable snapshot; no cross-service field joins.
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFoundError("Test run not found")
    request_scope = [ObservedRequest.run_id == run_id]
    if filters.request_id is not None:
        request_scope.append(ObservedRequest.id == filters.request_id)
        if not await session.scalar(select(ObservedRequest.id).where(*request_scope)):
            raise NotFoundError("Request not found")

    evidence_join = and_(RequestFieldEvidence.request_id == RequestFieldUsage.request_id,
                         RequestFieldEvidence.field_id == RequestFieldUsage.field_id)
    unavailable = select(1).select_from(RequestFieldUsage).outerjoin(RequestFieldEvidence, evidence_join).where(
        RequestFieldUsage.request_id == ObservedRequest.id,
        func.coalesce(RequestFieldEvidence.state, "unavailable") == "unavailable").exists()
    counts = select(
        func.count().label("request_count"),
        func.count().filter(ObservedRequest.analysis_status != "success").label("selection_unavailable_request_count"),
        func.count().filter((ObservedRequest.field_evidence_version.is_(None)) | unavailable).label("evidence_unavailable_request_count"),
    ).where(*request_scope).subquery()

    state = func.coalesce(RequestFieldEvidence.state, "unavailable")
    grouped = select(
        SchemaField.id.label("field_id"), SchemaType.name.label("type_name"), SchemaField.name.label("field_name"),
        func.count().label("selected_request_count"),
        func.count().filter(counted_observation()).label("selected_hits"),
        func.count().filter(state == "observed").label("observed_hits"),
        func.count().filter(state == "not_observed").label("not_observed_count"),
        func.count().filter(state == "indeterminate").label("indeterminate_count"),
        func.count().filter(state == "unavailable").label("unavailable_count"),
    ).select_from(ObservedRequest).join(RequestFieldUsage, RequestFieldUsage.request_id == ObservedRequest.id
    ).join(SchemaField, SchemaField.id == RequestFieldUsage.field_id
    ).join(SchemaType, SchemaType.id == SchemaField.type_id
    ).outerjoin(RequestFieldEvidence, and_(RequestFieldEvidence.request_id == RequestFieldUsage.request_id,
                                          RequestFieldEvidence.field_id == RequestFieldUsage.field_id)
    ).where(*request_scope, SchemaType.snapshot_id == run.snapshot_id,
            ObservedRequest.analysis_status == "success")
    if filters.type_name is not None:
        grouped = grouped.where(SchemaType.name == filters.type_name)
    if filters.field_name is not None:
        grouped = grouped.where(SchemaField.name == filters.field_name)
    grouped = grouped.group_by(SchemaField.id, SchemaType.name, SchemaField.name)
    if filters.state is not None:
        grouped = grouped.having(func.count().filter(state == filters.state) > 0)
    fields = grouped.cte("evidence_fields")
    # One statement returns request metrics, total and page in one MVCC view,
    # even if offset is past the last row of a concurrently running capture.
    page = select(fields).order_by(fields.c.type_name, fields.c.field_name, fields.c.field_id
        ).limit(filters.limit).offset(filters.offset).subquery()
    total = select(func.count().label("total")).select_from(fields).subquery()
    rows = (await session.execute(select(total.c.total, *counts.c, *page.c).select_from(total).join(counts, true()).outerjoin(
        page, true()).order_by(page.c.type_name, page.c.field_name, page.c.field_id))).mappings().all()
    return FieldEvidencePage(run_id=run_id, snapshot_id=run.snapshot_id, request_count=rows[0]["request_count"],
        selection_unavailable_request_count=rows[0]["selection_unavailable_request_count"],
        evidence_unavailable_request_count=rows[0]["evidence_unavailable_request_count"],
        total=rows[0]["total"], limit=filters.limit, offset=filters.offset,
        fields=[FieldEvidenceOut(**row) for row in rows if row["field_id"] is not None])
