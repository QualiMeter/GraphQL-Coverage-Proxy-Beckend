"""Database use cases; called by HTTP routes and future proxy integration."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_models import (
    CoverageGraph, CoverageSummary, GraphField, GraphRelation, GraphType,
    RequestOut, RunOut, SchemaOut, ServiceOut,
)
from app.contracts import RequestObservationInput
from app.models import (
    GraphQLService, ObservedRequest, RequestFieldUsage, SchemaField,
    SchemaSnapshot, SchemaType, TestRun,
)
from app.schema_collector import collect_schema
from app.schema_parser import ParsedSchema, parse_sdl


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class InvalidReferenceError(Exception):
    pass


def service_out(row: GraphQLService) -> ServiceOut:
    return ServiceOut(id=row.id, key=row.key, name=row.name, created_at=row.created_at)


def schema_out(row: SchemaSnapshot) -> SchemaOut:
    return SchemaOut(
        id=row.id, service_id=row.service_id, schema_hash=row.schema_hash,
        source=row.source, created_at=row.created_at,
    )


def run_out(row: TestRun) -> RunOut:
    return RunOut(
        id=row.id, snapshot_id=row.snapshot_id, name=row.name, status=row.status,
        started_at=row.started_at, finished_at=row.finished_at,
        metadata=row.metadata_json,
    )


async def create_service(session: AsyncSession, key: str, name: str) -> ServiceOut:
    existing = await session.scalar(select(GraphQLService.id).where(GraphQLService.key == key))
    if existing is not None:
        raise ConflictError("A service with that key already exists")
    row = GraphQLService(key=key, name=name)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("A service with that key already exists") from exc
    await session.refresh(row)
    return service_out(row)


async def list_services(session: AsyncSession) -> list[ServiceOut]:
    rows = (await session.scalars(select(GraphQLService).order_by(GraphQLService.name))).all()
    return [service_out(row) for row in rows]


async def _save_parsed_schema(
    session: AsyncSession,
    service_id: uuid.UUID,
    parsed: ParsedSchema,
    *,
    source: str,
    introspection_json: dict | None = None,
) -> SchemaOut:
    existing = await session.scalar(
        select(SchemaSnapshot).where(
            SchemaSnapshot.service_id == service_id,
            SchemaSnapshot.schema_hash == parsed.sha256,
        )
    )
    if existing is not None:
        if source == "introspection":
            existing.schema_sdl = parsed.canonical_sdl
            existing.source = source
            existing.introspection_json = introspection_json
            await session.commit()
            await session.refresh(existing)
        return schema_out(existing)

    snapshot = SchemaSnapshot(
        service_id=service_id, schema_hash=parsed.sha256,
        schema_sdl=parsed.canonical_sdl, source=source,
        introspection_json=introspection_json,
    )
    session.add(snapshot)
    await session.flush()

    type_ids: dict[str, uuid.UUID] = {}
    for item in parsed.types:
        type_id = uuid.uuid4()
        type_ids[item.name] = type_id
        session.add(SchemaType(id=type_id, snapshot_id=snapshot.id, name=item.name, kind=item.kind))
    await session.flush()

    for field in parsed.fields:
        session.add(
            SchemaField(
                type_id=type_ids[field.type_name], name=field.name,
                return_type=field.return_type,
                target_type_id=type_ids.get(field.target_type_name) if field.target_type_name else None,
                is_deprecated=field.is_deprecated,
            )
        )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("Concurrent schema import conflicted; retry the request") from exc
    await session.refresh(snapshot)
    return schema_out(snapshot)


async def import_sdl(session: AsyncSession, service_id: uuid.UUID, sdl: str) -> SchemaOut:
    if await session.get(GraphQLService, service_id) is None:
        raise NotFoundError("GraphQL service not found")
    return await _save_parsed_schema(
        session, service_id, parse_sdl(sdl), source="sdl"
    )


async def import_introspection_schema(
    session: AsyncSession, service_id: uuid.UUID, target_url: str
) -> SchemaOut:
    """Fetch an upstream schema and persist its normalized SDL and raw JSON.

    ``target_url`` must come from application configuration, never from an API
    request body. The HTTP route and configuration are owned by backend #1.
    """
    # Fetch first, before opening a database transaction. Never roll back the
    # caller's session: it may contain unrelated uncommitted changes.
    collected = await collect_schema(target_url)
    if await session.get(GraphQLService, service_id) is None:
        raise NotFoundError("GraphQL service not found")
    # The uniqueness rule is per service and normalized schema hash, so an
    # introspection of a schema already imported from SDL enriches that same
    # snapshot instead of creating a duplicate. The schema and provenance are
    # committed atomically.
    return await _save_parsed_schema(
        session,
        service_id,
        collected.parsed,
        source="introspection",
        introspection_json=collected.introspection_json,
    )


async def list_schemas(session: AsyncSession, service_id: uuid.UUID) -> list[SchemaOut]:
    rows = (await session.scalars(
        select(SchemaSnapshot)
        .where(SchemaSnapshot.service_id == service_id)
        .order_by(SchemaSnapshot.created_at.desc())
    )).all()
    return [schema_out(row) for row in rows]


async def create_run(
    session: AsyncSession, snapshot_id: uuid.UUID, name: str, metadata: dict
) -> RunOut:
    if await session.get(SchemaSnapshot, snapshot_id) is None:
        raise NotFoundError("Schema snapshot not found")
    existing = await session.scalar(
        select(TestRun.id).where(TestRun.snapshot_id == snapshot_id, TestRun.status == "running")
    )
    if existing is not None:
        raise ConflictError("A run is already active for this schema snapshot")
    row = TestRun(snapshot_id=snapshot_id, name=name, status="running", metadata_json=metadata)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("A run is already active for this schema snapshot") from exc
    await session.refresh(row)
    return run_out(row)


async def list_runs(session: AsyncSession, snapshot_id: uuid.UUID) -> list[RunOut]:
    rows = (await session.scalars(
        select(TestRun).where(TestRun.snapshot_id == snapshot_id)
        .order_by(TestRun.started_at.desc())
    )).all()
    return [run_out(row) for row in rows]


async def finish_run(session: AsyncSession, run_id: uuid.UUID) -> RunOut:
    # Row locking also prevents a concurrent request from recording after completion.
    run = await session.scalar(select(TestRun).where(TestRun.id == run_id).with_for_update())
    if run is None:
        raise NotFoundError("Test run not found")
    if run.status != "running":
        raise ConflictError("The run has already finished")
    run.status = "completed"
    run.finished_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(run)
    return run_out(run)


async def record_observation(session: AsyncSession, observation: RequestObservationInput) -> uuid.UUID:
    """Internal API for the future proxy; never accepts unverified field IDs from a client.

    The proxy must supply truthful upstream metadata. Cross-snapshot field
    references are rejected atomically. A failed analysis may have no fields.
    """
    run = await session.scalar(select(TestRun).where(TestRun.id == observation.run_id).with_for_update())
    if run is None:
        raise NotFoundError("Test run not found")
    if run.status != "running":
        raise ConflictError("Cannot record observations for a finished run")

    analysis = observation.analysis
    if analysis.status not in {
        "success", "parse_error", "validation_error", "unsupported", "internal_error"
    }:
        raise InvalidReferenceError("Unknown analysis status")
    if analysis.status != "success" and analysis.fields:
        raise InvalidReferenceError("Failed analysis cannot claim covered fields")

    field_ids: dict[tuple[str, str], uuid.UUID] = {}
    if analysis.fields:
        rows = (await session.execute(
            select(SchemaType.name, SchemaField.name, SchemaField.id)
            .join(SchemaField, SchemaField.type_id == SchemaType.id)
            .where(SchemaType.snapshot_id == run.snapshot_id)
        )).all()
        field_ids = {(type_name, field_name): field_id for type_name, field_name, field_id in rows}
        missing = {
            (item.type_name, item.field_name)
            for item in analysis.fields
            if (item.type_name, item.field_name) not in field_ids
        }
        if missing:
            raise InvalidReferenceError(f"Fields are absent from the run schema: {sorted(missing)}")

    row = ObservedRequest(
        run_id=observation.run_id,
        operation_name=analysis.operation_name,
        operation_type=analysis.operation_type,
        query_hash=observation.query_hash,
        http_method=observation.http_method,
        http_status=observation.http_status,
        analysis_status=analysis.status,
        response_has_data=observation.response_has_data,
        graphql_has_errors=observation.graphql_has_errors,
        duration_ms=observation.duration_ms,
        error_code=analysis.error_code,
    )
    session.add(row)
    await session.flush()
    for key in {(item.type_name, item.field_name) for item in analysis.fields}:
        session.add(RequestFieldUsage(request_id=row.id, field_id=field_ids[key]))
    await session.commit()
    return row.id


async def get_graph(
    session: AsyncSession, snapshot_id: uuid.UUID, run_id: uuid.UUID | None = None
) -> CoverageGraph:
    if await session.get(SchemaSnapshot, snapshot_id) is None:
        raise NotFoundError("Schema snapshot not found")

    hit_counts: dict[uuid.UUID, int] = {}
    if run_id is not None:
        run = await session.get(TestRun, run_id)
        if run is None or run.snapshot_id != snapshot_id:
            raise NotFoundError("Test run for this schema not found")
        counts = (await session.execute(
            select(RequestFieldUsage.field_id, func.count())
            .join(ObservedRequest, ObservedRequest.id == RequestFieldUsage.request_id)
            .where(
                ObservedRequest.run_id == run_id,
                ObservedRequest.analysis_status == "success",
                ObservedRequest.response_has_data.is_(True),
            )
            .group_by(RequestFieldUsage.field_id)
        )).all()
        hit_counts = {field_id: count for field_id, count in counts}

    type_rows = (await session.scalars(
        select(SchemaType).where(SchemaType.snapshot_id == snapshot_id)
        .order_by(SchemaType.name)
    )).all()
    field_rows = (await session.scalars(
        select(SchemaField).join(SchemaType, SchemaType.id == SchemaField.type_id)
        .where(SchemaType.snapshot_id == snapshot_id).order_by(SchemaField.name)
    )).all()
    type_by_id = {row.id: row for row in type_rows}
    fields_by_type: dict[uuid.UUID, list[SchemaField]] = {}
    for field in field_rows:
        fields_by_type.setdefault(field.type_id, []).append(field)

    graph_types: list[GraphType] = []
    relations: list[GraphRelation] = []
    total_fields = 0
    covered_fields = 0
    for type_row in type_rows:
        if type_row.kind not in {"OBJECT", "INTERFACE", "UNION"}:
            continue
        raw_fields = fields_by_type.get(type_row.id, [])
        graph_fields: list[GraphField] = []
        type_covered = 0
        for field in raw_fields:
            hits = hit_counts.get(field.id, 0)
            covered = hits > 0
            type_covered += int(covered)
            target = type_by_id.get(field.target_type_id) if field.target_type_id else None
            graph_fields.append(GraphField(
                name=field.name, return_type=field.return_type,
                target_type=target.name if target else None,
                deprecated=field.is_deprecated, covered=covered, hits=hits,
            ))
            if target is not None:
                relations.append(GraphRelation(
                    from_type=type_row.name, field=field.name,
                    to_type=target.name, covered=covered,
                ))
        if type_row.kind == "OBJECT":
            total_fields += len(raw_fields)
            covered_fields += type_covered
        graph_types.append(GraphType(
            name=type_row.name, kind=type_row.kind,
            total_fields=len(raw_fields), covered_fields=type_covered,
            coverage_percent=round(type_covered * 100 / len(raw_fields), 2) if raw_fields else None,
            fields=graph_fields,
        ))

    return CoverageGraph(
        schema_id=snapshot_id, run_id=run_id,
        summary=CoverageSummary(
            total_fields=total_fields, covered_fields=covered_fields,
            coverage_percent=round(covered_fields * 100 / total_fields, 2) if total_fields else 0.0,
        ),
        types=graph_types, relations=relations,
    )


async def get_run_graph(session: AsyncSession, run_id: uuid.UUID) -> CoverageGraph:
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFoundError("Test run not found")
    return await get_graph(session, run.snapshot_id, run_id)


async def list_requests(
    session: AsyncSession, run_id: uuid.UUID, limit: int, offset: int
) -> list[RequestOut]:
    if await session.get(TestRun, run_id) is None:
        raise NotFoundError("Test run not found")
    rows = (await session.scalars(
        select(ObservedRequest).where(ObservedRequest.run_id == run_id)
        .order_by(ObservedRequest.created_at.desc(), ObservedRequest.id.desc())
        .limit(limit).offset(offset)
    )).all()
    return [RequestOut(
        id=row.id, run_id=row.run_id, operation_name=row.operation_name,
        operation_type=row.operation_type, http_method=row.http_method,
        http_status=row.http_status, analysis_status=row.analysis_status,
        response_has_data=row.response_has_data,
        graphql_has_errors=row.graphql_has_errors,
        duration_ms=row.duration_ms, error_code=row.error_code, created_at=row.created_at,
    ) for row in rows]
