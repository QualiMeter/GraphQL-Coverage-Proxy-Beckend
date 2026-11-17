"""Read-only, batched comparison of completed runs for one schema snapshot."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_models import (
    ComparedFieldOut,
    ComparedFieldPageOut,
    ComparedTypeOut,
    ComparedTypePageOut,
    ComparisonDeltaOut,
    RunComparisonRequest,
    RunComparisonResponse,
    RunFieldHitsOut,
    RunMetricOut,
    TypeRunCoverageOut,
    AnalysisStatusCounts,
)
from app.auth import Principal
from app.auth_models import ServiceGrant
from app.models import (
    GraphQLService,
    ObservedRequest,
    RequestFieldUsage,
    SchemaField,
    SchemaSnapshot,
    SchemaType,
    TestRun,
)
from app.schema_cache import schema_from_sdl
from app.services import NotFoundError, counted_observation


class RunComparisonError(Exception):
    """A safe, stable API error raised by comparison selection checks."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class _RunRow:
    id: uuid.UUID
    snapshot_id: uuid.UUID
    name: str
    status: str
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class _SnapshotRow:
    id: uuid.UUID
    schema_hash: str
    schema_sdl: str


def _visible_service_filter(principal: Principal):
    if principal.admin:
        return True
    public = GraphQLService.is_public.is_(True)
    if principal.user_id is None:
        return public
    grant = exists(select(1).where(
        ServiceGrant.service_id == GraphQLService.id,
        ServiceGrant.user_id == principal.user_id,
    ))
    return and_(public | grant)


def _round(value):
    return round(float(value), 2) if value is not None else None


async def compare_run_series(
    session: AsyncSession,
    payload: RunComparisonRequest,
    principal: Principal,
) -> RunComparisonResponse:
    """Aggregate a selected run series without loading requests into Python."""
    selected_ids = payload.run_ids
    statement = (
        select(
            TestRun.id.label("run_id"), TestRun.snapshot_id, TestRun.name, TestRun.status,
            TestRun.started_at, TestRun.finished_at,
            SchemaSnapshot.id.label("snapshot_uuid"), SchemaSnapshot.schema_hash,
            SchemaSnapshot.schema_sdl,
        )
        .join(SchemaSnapshot, SchemaSnapshot.id == TestRun.snapshot_id)
        .join(GraphQLService, GraphQLService.id == SchemaSnapshot.service_id)
        .where(TestRun.id.in_(selected_ids), _visible_service_filter(principal))
        .order_by(TestRun.started_at, TestRun.id)
    )
    selected = (await session.execute(statement)).mappings().all()
    # This intentionally makes missing and inaccessible IDs indistinguishable.
    if len(selected) != len(selected_ids):
        raise NotFoundError("Test run not found")

    runs = [_RunRow(
        id=row["run_id"], snapshot_id=row["snapshot_id"], name=row["name"],
        status=row["status"], started_at=row["started_at"], finished_at=row["finished_at"],
    ) for row in selected]
    snapshot_ids = {run.snapshot_id for run in runs}
    if len(snapshot_ids) != 1:
        raise RunComparisonError("SNAPSHOT_MISMATCH", "All selected runs must use the same schema snapshot")
    if any(run.status != "completed" for run in runs):
        raise RunComparisonError("RUN_NOT_COMPLETED", "All selected runs must be completed")

    by_id = {run.id: run for run in runs}
    baseline = by_id[payload.baseline_run_id]
    target_id = payload.target_run_id
    if target_id is None:
        target_id = next(run.id for run in reversed(runs) if run.id != baseline.id)
    target = by_id[target_id]

    snapshot = _SnapshotRow(
        id=selected[0]["snapshot_uuid"], schema_hash=selected[0]["schema_hash"],
        schema_sdl=selected[0]["schema_sdl"],
    )
    # Denominator includes every persisted OBJECT field, including root fields.
    field_rows = (await session.execute(
        select(SchemaType.id.label("type_id"), SchemaType.name.label("type_name"),
               SchemaField.id.label("field_id"), SchemaField.name.label("field_name"))
        .join(SchemaField, SchemaField.type_id == SchemaType.id)
        .where(SchemaType.snapshot_id == snapshot.id, SchemaType.kind == "OBJECT")
        .order_by(SchemaType.name, SchemaField.name)
    )).mappings().all()

    # Aggregate counted hits in one query. RequestFieldUsage's composite key
    # guarantees one hit per field per request, even for repeated selections.
    hit_rows = (await session.execute(
        select(RequestFieldUsage.field_id, ObservedRequest.run_id, func.count().label("hits"))
        .join(ObservedRequest, ObservedRequest.id == RequestFieldUsage.request_id)
        .join(SchemaField, SchemaField.id == RequestFieldUsage.field_id)
        .join(SchemaType, SchemaType.id == SchemaField.type_id)
        .where(ObservedRequest.run_id.in_(selected_ids), counted_observation(),
               SchemaType.snapshot_id == snapshot.id, SchemaType.kind == "OBJECT")
        .group_by(RequestFieldUsage.field_id, ObservedRequest.run_id)
    )).mappings().all()
    hits_by_field: dict[uuid.UUID, dict[uuid.UUID, int]] = {}
    for row in hit_rows:
        hits_by_field.setdefault(row["field_id"], {})[row["run_id"]] = row["hits"]

    statuses = ("success", "parse_error", "validation_error", "unsupported", "internal_error")
    request_metric_rows = (await session.execute(
        select(
            ObservedRequest.run_id,
            func.count().label("total_requests"),
            func.count().filter(counted_observation()).label("counted_requests"),
            func.count().filter(ObservedRequest.graphql_has_errors.is_(True)).label("graphql_error_count"),
            func.count().filter(ObservedRequest.http_status.between(400, 599)).label("http_error_count"),
            func.avg(ObservedRequest.duration_ms).label("avg_duration_ms"),
            func.percentile_cont(0.95).within_group(ObservedRequest.duration_ms)
                .filter(ObservedRequest.duration_ms.is_not(None)).label("p95_duration_ms"),
            *[func.count().filter(ObservedRequest.analysis_status == status).label(status)
              for status in statuses],
            *[func.count().filter(ObservedRequest.operation_type == operation).label(f"{operation}_requests")
              for operation in ("query", "mutation", "subscription")],
        )
        .where(ObservedRequest.run_id.in_(selected_ids))
        .group_by(ObservedRequest.run_id)
    )).mappings().all()
    request_metrics = {row["run_id"]: row for row in request_metric_rows}

    operation_identities = (
        select(ObservedRequest.run_id)
        .where(ObservedRequest.run_id.in_(selected_ids),
               ObservedRequest.query_hash.is_not(None), ObservedRequest.operation_type.is_not(None))
        .group_by(ObservedRequest.run_id, ObservedRequest.query_hash,
                  ObservedRequest.operation_name, ObservedRequest.operation_type)
        .subquery()
    )
    operation_rows = (await session.execute(
        select(operation_identities.c.run_id, func.count().label("unique_operations"))
        .group_by(operation_identities.c.run_id)
    )).all()
    unique_operations: dict[uuid.UUID, int] = {run_id: 0 for run_id in selected_ids}
    for row in operation_rows:
        unique_operations[row.run_id] = row.unique_operations

    total_by_type: dict[str, int] = {}
    field_data = []
    for row in field_rows:
        total_by_type[row["type_name"]] = total_by_type.get(row["type_name"], 0) + 1
        per_run_hits = [hits_by_field.get(row["field_id"], {}).get(run.id, 0) for run in runs]
        covered = [value > 0 for value in per_run_hits]
        transitions = sum(left != right for left, right in zip(covered, covered[1:]))
        baseline_covered = hits_by_field.get(row["field_id"], {}).get(baseline.id, 0) > 0
        target_covered = hits_by_field.get(row["field_id"], {}).get(target.id, 0) > 0
        if not baseline_covered and target_covered:
            pair_category = "newlyCovered"
        elif baseline_covered and not target_covered:
            pair_category = "noLongerCovered"
        elif baseline_covered:
            pair_category = "stillCovered"
        else:
            pair_category = "stillUncovered"
        covered_count = sum(covered)
        if covered_count == len(runs):
            stability = "alwaysCovered"
        elif covered_count == 0:
            stability = "neverCovered"
        else:
            stability = "intermittentlyCovered"
        covered_positions = [index for index, is_covered in enumerate(covered) if is_covered]
        field_data.append({
            "type_name": row["type_name"], "field_name": row["field_name"],
            "field_id": row["field_id"], "type_id": row["type_id"],
            "hits": per_run_hits, "covered": covered,
            "covered_count": covered_count, "transitions": transitions,
            "pair_category": pair_category, "stability": stability,
            "first_index": covered_positions[0] if covered_positions else None,
            "last_index": covered_positions[-1] if covered_positions else None,
        })

    field_categories = {
        category: sum(item["pair_category"] == category for item in field_data)
        for category in ("newlyCovered", "noLongerCovered", "stillCovered", "stillUncovered")
    }
    field_categories.update({
        category: sum(item["stability"] == category for item in field_data)
        for category in ("alwaysCovered", "neverCovered", "intermittentlyCovered")
    })

    run_out: list[RunMetricOut] = []
    run_index_by_id = {run.id: index for index, run in enumerate(runs)}
    covered_type_fields: dict[tuple[str, uuid.UUID], set[str]] = {}
    for item in field_data:
        for index, run in enumerate(runs):
            if item["hits"][index] > 0:
                covered_type_fields.setdefault((item["type_name"], run.id), set()).add(item["field_name"])
    for run in runs:
        total_fields = len(field_data)
        run_index = run_index_by_id[run.id]
        covered_fields = sum(item["hits"][run_index] > 0 for item in field_data)
        coverage_percent = round(covered_fields * 100 / total_fields, 2) if total_fields else 0.0
        metrics = request_metrics.get(run.id)
        values = {status: metrics[status] if metrics else 0 for status in statuses}
        run_out.append(RunMetricOut(
            run_id=run.id, name=run.name, status="completed", started_at=run.started_at,
            finished_at=run.finished_at, total_fields=total_fields, covered_fields=covered_fields,
            coverage_percent=coverage_percent,
            total_requests=metrics["total_requests"] if metrics else 0,
            counted_requests=metrics["counted_requests"] if metrics else 0,
            unique_operations=unique_operations[run.id],
            query_requests=metrics["query_requests"] if metrics else 0,
            mutation_requests=metrics["mutation_requests"] if metrics else 0,
            subscription_requests=metrics["subscription_requests"] if metrics else 0,
            http_error_count=metrics["http_error_count"] if metrics else 0,
            graphql_error_count=metrics["graphql_error_count"] if metrics else 0,
            analysis_status_counts=AnalysisStatusCounts(**{
                "parse_error": values["parse_error"], "validation_error": values["validation_error"],
                "internal_error": values["internal_error"], "unsupported": values["unsupported"],
                "success": values["success"],
            }),
            avg_duration_ms=metrics["avg_duration_ms"] if metrics else None,
            p95_duration_ms=metrics["p95_duration_ms"] if metrics else None,
        ))

    def metric_delta(key: str):
        left, right = getattr(next(row for row in run_out if row.run_id == baseline.id), key), \
            getattr(next(row for row in run_out if row.run_id == target.id), key)
        return _round(right - left) if left is not None and right is not None else None

    adjacent_deltas = [{
        "fromRunId": str(left.run_id), "toRunId": str(right.run_id),
        "deltaPp": round(right.coverage_percent - left.coverage_percent, 2),
    } for left, right in zip(run_out, run_out[1:])]
    comparison = ComparisonDeltaOut(
        percentage_point_delta=round(
            next(row.coverage_percent for row in run_out if row.run_id == target.id)
            - next(row.coverage_percent for row in run_out if row.run_id == baseline.id), 2),
        covered_fields_delta=next(row.covered_fields for row in run_out if row.run_id == target.id)
            - next(row.covered_fields for row in run_out if row.run_id == baseline.id),
        requests_delta=metric_delta("total_requests"),
        counted_requests_delta=metric_delta("counted_requests"),
        unique_operations_delta=metric_delta("unique_operations"),
        http_error_count_delta=metric_delta("http_error_count"),
        graphql_error_count_delta=metric_delta("graphql_error_count"),
        avg_duration_delta_ms=metric_delta("avg_duration_ms"),
        p95_duration_delta_ms=metric_delta("p95_duration_ms"),
        adjacent_coverage_deltas_pp=adjacent_deltas,
        **field_categories,
    )

    field_filter = payload.fields.filter
    filtered_fields = [item for item in field_data if field_filter == "all"
        or (field_filter == "changed" and item["pair_category"] in {"newlyCovered", "noLongerCovered"})
        or item["pair_category"] == field_filter or item["stability"] == field_filter]
    field_total = len(filtered_fields)
    field_items = [ComparedFieldOut(
        type_name=item["type_name"], field_name=item["field_name"],
        hits_by_run=[RunFieldHitsOut(run_id=run.id, hits=item["hits"][index])
                     for index, run in enumerate(runs)],
        covered_run_count=item["covered_count"],
        coverage_frequency_percent=round(item["covered_count"] * 100 / len(runs), 2),
        coverage_transitions=item["transitions"], baseline_to_target=item["pair_category"],
        stability=item["stability"],
        first_covered_run_id=runs[item["first_index"]].id if item["first_index"] is not None else None,
        last_covered_run_id=runs[item["last_index"]].id if item["last_index"] is not None else None,
    ) for item in filtered_fields[payload.fields.offset:payload.fields.offset + payload.fields.limit]]

    schema = schema_from_sdl(snapshot.schema_sdl)
    roots = {
        "query": schema.query_type.name if schema.query_type else None,
        "mutation": schema.mutation_type.name if schema.mutation_type else None,
        "subscription": schema.subscription_type.name if schema.subscription_type else None,
    }
    type_names = sorted(total_by_type)
    type_items = [ComparedTypeOut(
        type_name=name,
        root_role=next((role for role, root_name in roots.items() if root_name == name), None),
        total_fields=total_by_type[name],
        runs=[TypeRunCoverageOut(
            run_id=run.id,
            covered_fields=len(covered_type_fields.get((name, run.id), set())),
            coverage_percent=round(
                len(covered_type_fields.get((name, run.id), set())) * 100 / total_by_type[name], 2
            ) if total_by_type[name] else 0.0,
        ) for run in runs],
    ) for name in type_names[payload.types.offset:payload.types.offset + payload.types.limit]]

    return RunComparisonResponse(
        snapshot_id=snapshot.id, schema_hash=snapshot.schema_hash,
        generated_at=datetime.now(timezone.utc), baseline_run_id=baseline.id,
        target_run_id=target.id, runs=run_out, comparison=comparison,
        field_categories=field_categories,
        fields=ComparedFieldPageOut(total=field_total, items=field_items),
        types=ComparedTypePageOut(total=len(type_names), items=type_items),
    )
