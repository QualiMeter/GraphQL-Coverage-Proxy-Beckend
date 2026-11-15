"""Database use cases; called by HTTP routes and future proxy integration."""

import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone

from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_models import (
    CoverageGraph, CoverageSummary, GraphField, GraphRelation, GraphType,
    RequestOut, RunOut, SchemaOut, SchemaSdlOut, ServiceOut,
)
from app.contracts import RequestObservationInput
from app.auth_models import ServiceGrant
from app.models import (
    GraphQLService, ObservedRequest, RequestFieldUsage, SchemaField,
    SchemaSnapshot, SchemaType, TestRun,
)
from app.schema_metadata import schema_metadata, root_types, argument_metadata, abstract_metadata
from app.schema_collector import collect_schema
from app.config import target_graphql_url
from app.upstream import legacy_enabled
from app.schema_parser import ParsedSchema, parse_sdl
from app.schema_work import schema_work


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class SchemaEndpointNotConfigured(ConflictError):
    pass


class SchemaEndpointChanged(ConflictError):
    pass


_UNCHECKED_ENDPOINT = object()
SessionProvider = Callable[[], AbstractAsyncContextManager[AsyncSession]]
SchemaPermissionCheck = Callable[[AsyncSession], Awaitable[None]]


class InvalidReferenceError(Exception):
    pass


def service_out(row: GraphQLService, *, include_endpoint: bool = True) -> ServiceOut:
    return ServiceOut(
        id=row.id, key=row.key, name=row.name,
        is_public=row.is_public, created_at=row.created_at, updated_at=row.updated_at,
        description=row.description, endpoint_url=row.endpoint_url if include_endpoint else None,
    )


def schema_out(row: SchemaSnapshot) -> SchemaOut:
    return SchemaOut(
        id=row.id, service_id=row.service_id, schema_hash=row.schema_hash,
        source=row.source, created_at=row.created_at, display_name=row.display_name,
    )


def run_out(row: TestRun, *, include_metadata: bool = True) -> RunOut:
    return RunOut(
        id=row.id, snapshot_id=row.snapshot_id, name=row.name, status=row.status,
        started_at=row.started_at, finished_at=row.finished_at,
        # Untrusted CI metadata may contain internal details and is never public.
        metadata=row.metadata_json if include_metadata else {},
    )


async def create_service(
    session: AsyncSession, key: str, name: str, is_public: bool = False,
    *, owner_id: uuid.UUID | None = None, endpoint_url: str | None = None, description: str | None = None,
) -> ServiceOut:
    existing = await session.scalar(select(GraphQLService.id).where(GraphQLService.key == key))
    if existing is not None:
        raise ConflictError("A service with that key already exists")
    row = GraphQLService(key=key, name=name, is_public=is_public, endpoint_url=endpoint_url, description=description)
    session.add(row)
    try:
        if owner_id is not None:
            await session.flush()
            session.add(ServiceGrant(user_id=owner_id, service_id=row.id))
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("A service with that key already exists") from exc
    await session.refresh(row)
    return service_out(row)


async def list_services(session: AsyncSession, *, public_only: bool = False, user_id: uuid.UUID | None = None, include_endpoint: bool = True) -> list[ServiceOut]:
    stmt = select(GraphQLService, _visible_service(user_id, require_grant=True)).order_by(GraphQLService.name, GraphQLService.id)
    if public_only:
        stmt = stmt.where(_visible_service(user_id))
    rows = (await session.execute(stmt)).all()
    return [service_out(row, include_endpoint=include_endpoint and (not public_only or granted))
            for row, granted in rows]


async def update_service(
    session: AsyncSession, service_id: uuid.UUID, changes: dict,
    *, authorize: Callable[[AsyncSession], Awaitable[None]] | None = None,
) -> ServiceOut:
    # All run creation and endpoint updates lock the same service row. This is
    # necessary even when the active run belongs to a different snapshot.
    row = await session.scalar(
        select(GraphQLService).where(GraphQLService.id == service_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundError("GraphQL service not found")
    if authorize is not None:
        await authorize(session)
    if "endpoint_url" in changes and changes["endpoint_url"] != row.endpoint_url:
        active = await session.scalar(select(TestRun.id).join(
            SchemaSnapshot, SchemaSnapshot.id == TestRun.snapshot_id,
        ).where(SchemaSnapshot.service_id == service_id, TestRun.status == "running").limit(1))
        if active is not None:
            raise ConflictError("Endpoint cannot change while the service has an active run")
    for key, value in changes.items():
        setattr(row, key, value)
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return service_out(row)


async def change_service_visibility(session: AsyncSession, service_id: uuid.UUID, is_public: bool) -> ServiceOut:
    return await update_service(session, service_id, {"is_public": is_public})


async def get_service(session: AsyncSession, service_id: uuid.UUID, *, include_endpoint: bool = True) -> ServiceOut:
    row = await session.get(GraphQLService, service_id)
    if row is None:
        raise NotFoundError("GraphQL service not found")
    return service_out(row, include_endpoint=include_endpoint)


def _visible_service(user_id: uuid.UUID | None, *, require_grant: bool = False):
    grant = (select(ServiceGrant.user_id).where(
        ServiceGrant.user_id == user_id, ServiceGrant.service_id == GraphQLService.id,
    ).exists() if user_id is not None else false())
    return grant if require_grant else or_(GraphQLService.is_public.is_(True), grant)


async def ensure_visible_service(
    session: AsyncSession, service_id: uuid.UUID, *, admin: bool, user_id: uuid.UUID | None = None,
    require_grant: bool = False,
) -> None:
    stmt = select(GraphQLService.id).where(GraphQLService.id == service_id)
    if not admin:
        stmt = stmt.where(_visible_service(user_id, require_grant=require_grant))
    if await session.scalar(stmt) is None:
        # Do not reveal the existence of private resources to anonymous callers.
        raise NotFoundError("GraphQL service not found")


async def ensure_visible_snapshot(
    session: AsyncSession, snapshot_id: uuid.UUID, *, admin: bool, user_id: uuid.UUID | None = None,
    require_grant: bool = False,
) -> None:
    service_id = await session.scalar(
        select(SchemaSnapshot.service_id).where(SchemaSnapshot.id == snapshot_id)
    )
    if service_id is None:
        raise NotFoundError("Schema snapshot not found")
    await ensure_visible_service(session, service_id, admin=admin, user_id=user_id, require_grant=require_grant)


async def ensure_visible_run(
    session: AsyncSession, run_id: uuid.UUID, *, admin: bool, user_id: uuid.UUID | None = None,
    require_grant: bool = False,
) -> None:
    service_id = await session.scalar(
        select(SchemaSnapshot.service_id)
        .join(TestRun, TestRun.snapshot_id == SchemaSnapshot.id)
        .where(TestRun.id == run_id)
    )
    if service_id is None:
        raise NotFoundError("Test run not found")
    await ensure_visible_service(session, service_id, admin=admin, user_id=user_id, require_grant=require_grant)


async def _save_parsed_schema(
    session: AsyncSession, service_id: uuid.UUID, parsed: ParsedSchema, *, source: str,
    introspection_json: dict | None = None, expected_endpoint=_UNCHECKED_ENDPOINT,
    authorize: SchemaPermissionCheck | None = None, commit: bool = True,
) -> SchemaOut:
    # Serialize SDL and introspection writes on the same service. The lock is
    # acquired only after HTTP/validation, never while waiting for an upstream.
    # Savepoint covers every flush, preserving unrelated caller work on failure.
    try:
        async with session.begin_nested():
            service = await session.scalar(select(GraphQLService).where(
                GraphQLService.id == service_id,
            ).with_for_update().execution_options(populate_existing=True))
            if service is None:
                raise NotFoundError("GraphQL service not found")
            if authorize is not None:
                await authorize(session)
            if expected_endpoint is not _UNCHECKED_ENDPOINT and service.endpoint_url != expected_endpoint:
                raise SchemaEndpointChanged("Service endpoint changed during introspection; repeat the import")
            snapshot = await session.scalar(select(SchemaSnapshot).where(
                SchemaSnapshot.service_id == service_id, SchemaSnapshot.schema_hash == parsed.sha256,
            ).execution_options(populate_existing=True))
            if snapshot is not None:
                if source == "introspection":
                    # Keep historical identity/SDL/types/fields/runs unchanged;
                    # enrich only schema provenance and introspection metadata.
                    snapshot.source = source
                    snapshot.introspection_json = introspection_json
            else:
                snapshot = SchemaSnapshot(service_id=service_id, schema_hash=parsed.sha256,
                    schema_sdl=parsed.canonical_sdl, source=source, introspection_json=introspection_json)
                session.add(snapshot)
                await session.flush()
                type_ids = {item.name: uuid.uuid4() for item in parsed.types}
                session.add_all([SchemaType(id=type_ids[item.name], snapshot_id=snapshot.id,
                    name=item.name, kind=item.kind) for item in parsed.types])
                await session.flush()
                session.add_all([SchemaField(type_id=type_ids[item.type_name], name=item.name,
                    return_type=item.return_type,
                    target_type_id=type_ids.get(item.target_type_name) if item.target_type_name else None,
                    is_deprecated=item.is_deprecated) for item in parsed.fields])
            await session.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise ConflictError("Concurrent schema import conflicted; retry the import") from None
        raise
    # Materialize before commit: refresh afterwards would start another
    # transaction and check out a connection again (also with expire_on_commit).
    result = schema_out(snapshot)
    if commit:
        await session.commit()
    return result


async def import_sdl(session: AsyncSession, service_id: uuid.UUID, sdl: str) -> SchemaOut:
    if await session.get(GraphQLService, service_id) is None:
        raise NotFoundError("GraphQL service not found")
    return await _save_parsed_schema(
        session, service_id, await schema_work(parse_sdl, sdl), source="sdl"
    )


async def import_sdl_schema(
    sessions: SessionProvider, service_id: uuid.UUID, sdl: str, *,
    authorize: SchemaPermissionCheck,
) -> SchemaOut:
    # Match introspection's separate read/compute/write phases. No caller-owned
    # transaction is committed or rolled back to release a connection.
    async with sessions() as session:
        await authorize(session)
        if await session.get(GraphQLService, service_id) is None:
            raise NotFoundError("GraphQL service not found")
    parsed = await schema_work(parse_sdl, sdl)
    async with sessions() as session:
        async with session.begin():
            return await _save_parsed_schema(
                session, service_id, parsed, source="sdl", authorize=authorize, commit=False,
            )


async def import_introspection_schema(
    sessions: SessionProvider, service_id: uuid.UUID, *, authorize: SchemaPermissionCheck | None = None,
) -> SchemaOut:
    """Own separate read/write sessions; retain no DB connection during HTTP.

    The factory must create a fresh session per call, not return a caller-owned
    active AsyncSession. We never commit/rollback any other session or pending
    caller work. API authorization runs in both phases; trusted internal callers
    may omit it. Uncommitted services require an explicit caller commit first.
    """
    async with sessions() as session:
        async with session.begin():
            if authorize is not None:
                await authorize(session)
            service = await session.get(GraphQLService, service_id)
            if service is None:
                raise NotFoundError("GraphQL service not found")
            endpoint = service.endpoint_url
            target = endpoint or (target_graphql_url() if legacy_enabled() else None)
            if not target:
                raise SchemaEndpointNotConfigured("Service endpoint is not configured; set endpointUrl or import SDL manually")
    # Both the read transaction and session have ended before DNS/HTTP/parsing.
    collected = await collect_schema(target)
    async with sessions() as session:
        async with session.begin():
            # Lock, reauthorize and compare the endpoint in the write transaction.
            # Keep all schema/type/field flushes protected by the existing savepoint.
            result = await _save_parsed_schema(
                session, service_id, collected.parsed, source="introspection",
                introspection_json=collected.introspection_json, expected_endpoint=endpoint,
                authorize=authorize, commit=False,
            )
    return result


async def get_schema_sdl(session: AsyncSession, snapshot_id: uuid.UUID) -> SchemaSdlOut:
    snapshot = await session.get(SchemaSnapshot, snapshot_id)
    if snapshot is None:
        raise NotFoundError("Schema snapshot not found")
    return SchemaSdlOut(schema_id=snapshot.id, schema_hash=snapshot.schema_hash, sdl=snapshot.schema_sdl)


async def update_snapshot_label(session: AsyncSession, snapshot_id: uuid.UUID, display_name: str | None) -> SchemaOut:
    snapshot = await session.scalar(select(SchemaSnapshot).where(
        SchemaSnapshot.id == snapshot_id,
    ).with_for_update())
    if snapshot is None:
        raise NotFoundError("Schema snapshot not found")
    snapshot.display_name = display_name
    result = schema_out(snapshot)
    await session.commit()
    return result


async def list_schemas(session: AsyncSession, service_id: uuid.UUID) -> list[SchemaOut]:
    rows = (await session.scalars(
        select(SchemaSnapshot)
        .where(SchemaSnapshot.service_id == service_id)
        .order_by(SchemaSnapshot.created_at.desc())
    )).all()
    return [schema_out(row) for row in rows]


async def create_run(
    session: AsyncSession, snapshot_id: uuid.UUID, name: str, metadata: dict,
    *, include_metadata: bool = True,
) -> RunOut:
    service = await session.scalar(select(GraphQLService).join(
        SchemaSnapshot, SchemaSnapshot.service_id == GraphQLService.id,
    ).where(SchemaSnapshot.id == snapshot_id).with_for_update(of=GraphQLService))
    if service is None:
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
    return run_out(row, include_metadata=include_metadata)


async def list_runs(
    session: AsyncSession, snapshot_id: uuid.UUID, *, include_metadata: bool = True
) -> list[RunOut]:
    from app.journal import RunFilters, list_run_page
    rows, _ = await list_run_page(session, RunFilters(snapshot_id=snapshot_id), include_metadata=include_metadata)
    return rows


async def finish_run(session: AsyncSession, run_id: uuid.UUID, *, include_metadata: bool = True) -> RunOut:
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
    return run_out(run, include_metadata=include_metadata)


async def record_observation(session: AsyncSession, observation: RequestObservationInput) -> uuid.UUID:
    """Internal API for the future proxy; never accepts unverified field IDs from a client.

    The proxy must supply truthful upstream metadata. Cross-snapshot field
    references are rejected atomically. A failed analysis may have no fields.
    """
    run = await session.scalar(
        select(TestRun).where(TestRun.id == observation.run_id).with_for_update()
        .execution_options(populate_existing=True)
    )
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
        safe_graphql_document=observation.safe_document.text,
        document_availability=observation.safe_document.availability,
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


COVERAGE_KINDS = frozenset({"OBJECT"})


def counted_observation():
    return and_(
        ObservedRequest.analysis_status == "success",
        ObservedRequest.response_has_data.is_(True),
    )


async def load_graph(
    session: AsyncSession, snapshot_id: uuid.UUID, run_id: uuid.UUID | None = None
) -> tuple:
    snapshot = await session.get(SchemaSnapshot, snapshot_id)
    if snapshot is None:
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
                counted_observation(),
            )
            .group_by(RequestFieldUsage.field_id)
        )).all()
        hit_counts = {field_id: count for field_id, count in counts}

    type_rows = (await session.execute(
        select(SchemaType.id, SchemaType.name, SchemaType.kind).where(SchemaType.snapshot_id == snapshot_id)
        .order_by(SchemaType.name)
    )).all()
    field_rows = (await session.execute(
        select(SchemaField.id, SchemaField.type_id, SchemaField.name, SchemaField.return_type,
               SchemaField.target_type_id, SchemaField.is_deprecated).join(SchemaType, SchemaType.id == SchemaField.type_id)
        .where(SchemaType.snapshot_id == snapshot_id).order_by(SchemaField.name)
    )).all()
    # SQL result tuples contain scalar values only, with no lazy ORM state.
    return snapshot_id, run_id, snapshot.schema_sdl, type_rows, field_rows, hit_counts


def coverage_summary(total_fields: int, covered_fields: int) -> CoverageSummary:
    return CoverageSummary(
        total_fields=total_fields, covered_fields=covered_fields,
        coverage_percent=round(covered_fields * 100 / total_fields, 2) if total_fields else 0.0,
    )


def build_graph(data: tuple) -> CoverageGraph:
    snapshot_id, run_id, sdl, type_rows, field_rows, hit_counts = data
    metadata = schema_metadata(sdl)
    roots = root_types(metadata)
    root_operations: dict[str, list[str]] = {}
    for kind, name in roots.model_dump().items():
        if name is not None:
            root_operations.setdefault(name, []).append(kind)

    type_by_id = {row.id: row for row in type_rows}
    fields_by_type: dict[uuid.UUID, list] = {}
    for field in field_rows:
        fields_by_type.setdefault(field.type_id, []).append(field)

    graph_types: list[GraphType] = []
    relations: list[GraphRelation] = []
    total_fields = 0
    covered_fields = 0
    for type_row in type_rows:
        if type_row.kind not in {"OBJECT", "INTERFACE", "UNION"}:
            continue
        gql_type = metadata.get_type(type_row.name)
        interfaces, possible_types = abstract_metadata(metadata, gql_type)
        raw_fields = fields_by_type.get(type_row.id, [])
        graph_fields: list[GraphField] = []
        type_covered = 0
        for field in raw_fields:
            hits = hit_counts.get(field.id, 0)
            covered = hits > 0
            type_covered += int(covered)
            target = type_by_id.get(field.target_type_id) if field.target_type_id else None
            gql_field = gql_type.fields[field.name]
            graph_fields.append(GraphField(
                description=gql_field.description, deprecation_reason=gql_field.deprecation_reason,
                args=argument_metadata(gql_field),
                name=field.name, return_type=field.return_type,
                target_type=target.name if target else None,
                deprecated=field.is_deprecated, covered=covered, hits=hits,
            ))
            if target is not None:
                relations.append(GraphRelation(
                    id=uuid.uuid5(snapshot_id, json.dumps([type_row.name, field.name, target.name], separators=(",", ":"))),
                    from_type=type_row.name, field=field.name,
                    to_type=target.name, covered=covered,
                ))
        if type_row.kind in COVERAGE_KINDS:
            total_fields += len(raw_fields)
            covered_fields += type_covered
        graph_types.append(GraphType(
            description=gql_type.description, root_operations=root_operations.get(type_row.name, []),
            interfaces=interfaces, possible_types=possible_types,
            name=type_row.name, kind=type_row.kind,
            total_fields=len(raw_fields), covered_fields=type_covered,
            coverage_percent=round(type_covered * 100 / len(raw_fields), 2) if raw_fields else None,
            fields=graph_fields,
        ))

    return CoverageGraph(
        schema_id=snapshot_id, run_id=run_id, root_types=roots,
        summary=coverage_summary(total_fields, covered_fields),
        types=graph_types, relations=relations,
    )


async def get_graph(
    session: AsyncSession, snapshot_id: uuid.UUID, run_id: uuid.UUID | None = None,
) -> CoverageGraph:
    # Internal callers retain ownership of their transaction. HTTP callers load
    # the same scalar data in a scoped session and close it before schema_work.
    return await schema_work(build_graph, await load_graph(session, snapshot_id, run_id))


async def get_run_graph(session: AsyncSession, run_id: uuid.UUID) -> CoverageGraph:
    run = await session.get(TestRun, run_id)
    if run is None:
        raise NotFoundError("Test run not found")
    return await get_graph(session, run.snapshot_id, run_id)


async def list_requests(
    session: AsyncSession, run_id: uuid.UUID, limit: int, offset: int
) -> list[RequestOut]:
    from app.journal import RequestFilters, list_request_page
    rows, _ = await list_request_page(session, run_id, RequestFilters(limit=limit, offset=offset))
    return rows
