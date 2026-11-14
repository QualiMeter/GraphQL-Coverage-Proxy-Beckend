"""Read-only run analytics over persisted observations; no request bodies needed."""

import hashlib
import json
import uuid
from datetime import timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_models import OperationFieldOut, OperationOut, RequestTimelineBucket, RunStats
from app.models import ObservedRequest, RequestFieldUsage, SchemaField, SchemaType, TestRun
from app.services import NotFoundError, get_run_graph


def operation_key(query_hash: str, operation_name: str | None, operation_type: str) -> str:
    # JSON preserves null versus a name and avoids ambiguous delimiter concatenation.
    identity = json.dumps(
        [query_hash, operation_name, operation_type], ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _counted():
    # Same eligibility as get_graph(). HTTP/GraphQL errors do not override data.
    return and_(
        ObservedRequest.analysis_status == "success",
        ObservedRequest.response_has_data.is_(True),
    )


def _metrics():
    return (
        func.count().label("request_count"),
        func.count().filter(_counted()).label("counted_request_count"),
        func.count().filter(ObservedRequest.graphql_has_errors.is_(True)).label("graphql_error_count"),
        func.count().filter(ObservedRequest.http_status.between(400, 599)).label("http_error_count"),
        func.avg(ObservedRequest.duration_ms).label("avg_duration_ms"),
    )


def _identity_columns():
    return (
        ObservedRequest.query_hash,
        ObservedRequest.operation_name,
        ObservedRequest.operation_type,
    )


def _identifiable():
    # Failed parsing and old observations may lack identity. Never invent one.
    return and_(
        ObservedRequest.query_hash.is_not(None),
        ObservedRequest.operation_type.is_not(None),
    )


async def _ensure_run(session: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFoundError("Test run not found")
    return run


async def list_operations(
    session: AsyncSession, run_id: uuid.UUID,
    operation_name: str | None = None, operation_type: str | None = None,
) -> list[OperationOut]:
    run = await _ensure_run(session, run_id)
    filters = [ObservedRequest.run_id == run_id, _identifiable()]
    if operation_name is not None:
        filters.append(ObservedRequest.operation_name == operation_name)
    if operation_type is not None:
        filters.append(ObservedRequest.operation_type == operation_type)

    identity = _identity_columns()
    # Request aggregates never JOIN field usage: one observation is one request.
    rows = (await session.execute(
        select(*identity, *_metrics(), func.max(ObservedRequest.created_at).label("last_seen_at"))
        .where(*filters).group_by(*identity)
        .order_by(*identity)
    )).mappings().all()

    field_rows = (await session.execute(
        select(*identity, SchemaType.name.label("type_name"), SchemaField.name.label("field_name"),
               func.count().label("request_count"))
        .select_from(ObservedRequest)
        .join(RequestFieldUsage, RequestFieldUsage.request_id == ObservedRequest.id)
        .join(SchemaField, SchemaField.id == RequestFieldUsage.field_id)
        .join(SchemaType, SchemaType.id == SchemaField.type_id)
        .where(*filters, SchemaType.snapshot_id == run.snapshot_id,
               ObservedRequest.analysis_status == "success")
        .group_by(*identity, SchemaType.name, SchemaField.name)
        .order_by(SchemaType.name, SchemaField.name)
    )).mappings().all()
    fields: dict[tuple, list[OperationFieldOut]] = {}
    for row in field_rows:
        key = (row["query_hash"], row["operation_name"], row["operation_type"])
        fields.setdefault(key, []).append(OperationFieldOut(
            type_name=row["type_name"], field_name=row["field_name"],
            request_count=row["request_count"],
        ))
    return [OperationOut(
        operation_key=operation_key(row["query_hash"], row["operation_name"], row["operation_type"]),
        operation_name=row["operation_name"], operation_type=row["operation_type"],
        request_count=row["request_count"], counted_request_count=row["counted_request_count"],
        graphql_error_count=row["graphql_error_count"], http_error_count=row["http_error_count"],
        avg_duration_ms=row["avg_duration_ms"], last_seen_at=row["last_seen_at"].astimezone(timezone.utc),
        fields=fields.get((row["query_hash"], row["operation_name"], row["operation_type"]), []),
    ) for row in rows]


async def get_run_stats(session: AsyncSession, run_id: uuid.UUID) -> RunStats:
    # Reuse Coverage Engine, including its denominator, rounding and empty schema semantics.
    graph = await get_run_graph(session, run_id)
    row = (await session.execute(
        select(
            *_metrics(),
            func.count().filter(ObservedRequest.operation_type == "query").label("query_requests"),
            func.count().filter(ObservedRequest.operation_type == "mutation").label("mutation_requests"),
            func.count().filter(ObservedRequest.operation_type == "subscription").label("subscription_requests"),
        ).where(ObservedRequest.run_id == run_id)
    )).mappings().one()
    identities = (
        select(*_identity_columns()).where(ObservedRequest.run_id == run_id, _identifiable())
        .group_by(*_identity_columns()).subquery()
    )
    unique_operations = await session.scalar(select(func.count()).select_from(identities))
    # Explicit timezone is essential: PostgreSQL/session timezone need not be UTC.
    bucket = func.date_trunc("minute", ObservedRequest.created_at, "UTC")
    timeline_rows = (await session.execute(
        select(bucket.label("bucket_start"), *_metrics())
        .where(ObservedRequest.run_id == run_id).group_by(bucket).order_by(bucket)
    )).mappings().all()
    return RunStats(
        **graph.summary.model_dump(),
        total_requests=row["request_count"], counted_requests=row["counted_request_count"],
        unique_operations=unique_operations, query_requests=row["query_requests"],
        mutation_requests=row["mutation_requests"], subscription_requests=row["subscription_requests"],
        http_error_count=row["http_error_count"], graphql_error_count=row["graphql_error_count"],
        avg_duration_ms=row["avg_duration_ms"],
        request_timeline=[RequestTimelineBucket(
            bucket_start=item["bucket_start"].astimezone(timezone.utc),
            request_count=item["request_count"], counted_request_count=item["counted_request_count"],
            graphql_error_count=item["graphql_error_count"], http_error_count=item["http_error_count"],
            avg_duration_ms=item["avg_duration_ms"],
        ) for item in timeline_rows],
    )
