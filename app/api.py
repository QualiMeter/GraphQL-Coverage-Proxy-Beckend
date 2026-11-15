"""Frontend-facing REST endpoints with role and service access checks."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from graphql import GraphQLError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_models import (
    CoverageGraph, RequestOut, RunCreate, RunOut, SchemaImport, SchemaIntrospectionImport,
    SchemaOut, SchemaImportErrorOut, ServiceCreate, ServiceOut, ServiceUpdate,
    OperationOut, OperationType, RunStats,
)
from app.access import require_operator, require_analyst, require_role
from app.auth import Principal, current_principal, visibility_kwargs
from app.upstream import UnsafeEndpoint, validate_endpoint
from app.database import get_session, get_session_factory
from app.run_analytics import get_run_stats, list_operations
from app.schema_collector import SchemaCollectionError
from app.services import (
    ConflictError, NotFoundError, SchemaEndpointChanged, SchemaEndpointNotConfigured, update_service, get_service, create_run, create_service,
    ensure_visible_run, ensure_visible_service, ensure_visible_snapshot, finish_run,
    get_graph, get_run_graph, import_introspection_schema, import_sdl,
    list_requests, list_runs, list_schemas, list_services,
)


router = APIRouter(prefix="/api", tags=["Coverage API"],
                   responses={401: {"description": "Authentication required for protected operations"},
                              403: {"description": "Insufficient role or invalid Origin/CSRF"}})
Session = Depends(get_session)


async def introspection_principal(request: Request, sessions=Depends(get_session_factory)) -> Principal:
    # Authentication must also release its connection before the endpoint waits
    # on HTTP. The ordinary request-scoped get_session dependency cannot do so.
    async with sessions() as session:
        return await current_principal(request, session)


require_introspection_operator = require_role("operator", principal_dependency=introspection_principal)


def api_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.post(
    "/services", status_code=201, response_model=ServiceOut,
)
async def post_service(payload: ServiceCreate, session: AsyncSession = Session,
                       principal: Principal = Depends(require_operator)):
    try:
        destination = await validate_endpoint(payload.endpoint_url)
        return await create_service(session, payload.key, payload.name, payload.is_public,
                                    owner_id=principal.user_id, endpoint_url=str(destination.url),
                                    description=payload.description)
    except UnsafeEndpoint:
        raise HTTPException(status_code=422, detail="Endpoint is not permitted by the outbound security policy") from None
    except ConflictError as exc:
        raise api_errors(exc) from exc


@router.get("/services", response_model=list[ServiceOut])
async def get_services(session: AsyncSession = Session, principal: Principal = Depends(current_principal)):
    kwargs = {"public_only": not principal.admin}
    if principal.user_id is not None and not principal.admin:
        kwargs["user_id"] = principal.user_id
    return await list_services(session, include_endpoint=principal.admin or principal.role == "operator", **kwargs)


@router.get("/services/{service_id}", response_model=ServiceOut)
async def get_service_detail(service_id: uuid.UUID, session: AsyncSession = Session,
                             principal: Principal = Depends(current_principal)):
    try:
        await ensure_visible_service(session, service_id, **visibility_kwargs(principal))
        include_endpoint = principal.admin
        if principal.role == "operator":
            try:
                await ensure_visible_service(session, service_id, require_grant=True, **visibility_kwargs(principal))
                include_endpoint = True
            except NotFoundError:
                pass
        return await get_service(session, service_id, include_endpoint=include_endpoint)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.patch(
    "/services/{service_id}", response_model=ServiceOut,
)
async def patch_service(
    service_id: uuid.UUID, payload: ServiceUpdate, session: AsyncSession = Session,
    principal: Principal = Depends(require_operator),
):
    try:
        await ensure_visible_service(session, service_id, require_grant=True, **visibility_kwargs(principal))
        changes = payload.model_dump(exclude_unset=True)
        if "endpoint_url" in changes:
            changes["endpoint_url"] = str((await validate_endpoint(changes["endpoint_url"])).url)
        return await update_service(session, service_id, changes)
    except UnsafeEndpoint:
        raise HTTPException(status_code=422, detail="Endpoint is not permitted by the outbound security policy") from None
    except (NotFoundError, ConflictError) as exc:
        raise api_errors(exc) from exc


@router.post(
    "/schemas/import", status_code=201, response_model=SchemaOut,
)
async def post_schema(payload: SchemaImport, session: AsyncSession = Session,
                      principal: Principal = Depends(require_operator)):
    try:
        if not principal.admin:
            await ensure_visible_service(session, payload.service_id, require_grant=True, **visibility_kwargs(principal))
        return await import_sdl(session, payload.service_id, payload.sdl)
    except (NotFoundError, ConflictError, ValueError, GraphQLError) as exc:
        raise api_errors(exc) from exc


@router.post(
    "/schemas/import/introspection", status_code=201, response_model=SchemaOut,
    responses={status: {"model": SchemaImportErrorOut, "description": description} for status, description in {
        404: "Service not found or inaccessible (SERVICE_NOT_FOUND)",
        409: "Endpoint missing/changed, destination forbidden or introspection disabled; use SDL when appropriate",
        502: "Upstream HTTP/DNS/JSON/schema/size failure; no upstream details are exposed",
        503: "Outbound capacity or schema storage unavailable",
        504: "DNS, connection or response timeout",
    }.items()},
    description="Import the selected Service endpointUrl using DNS/IP validation, pinned TLS transport, "
                "no redirects/retries/proxies or incoming credentials. Operator requires a service grant; admin may import any service. "
                "Deduplicates by serviceId + schemaHash; matching SDL snapshots retain identity and fields while source/introspection metadata are enriched. "
                "Errors preserve string detail and add a stable code. INTROSPECTION_DISABLED (409) recommends manual SDL import.",
)
async def post_introspection_schema(
    payload: SchemaIntrospectionImport, request: Request, sessions=Depends(get_session_factory),
    principal: Principal = Depends(require_introspection_operator),
):
    def failure(status: int, code: str, detail: str):
        return JSONResponse(status_code=status, content={"detail": detail, "code": code})
    async def authorize(session: AsyncSession):
        # Re-read role, session revocation/expiry and grant in each short phase;
        # the Principal captured before HTTP is not authorization for the write.
        current = await current_principal(request, session)
        await require_operator(request, principal=current)
        if not current.admin:
            await ensure_visible_service(session, payload.service_id, require_grant=True, **visibility_kwargs(current))

    try:
        return await import_introspection_schema(sessions, payload.service_id, authorize=authorize)
    except NotFoundError:
        return failure(404, "SERVICE_NOT_FOUND", "GraphQL service not found")
    except SchemaEndpointNotConfigured:
        return failure(409, "SERVICE_ENDPOINT_NOT_CONFIGURED", "Service endpoint is not configured; set endpointUrl or import SDL manually")
    except SchemaEndpointChanged:
        return failure(409, "SERVICE_ENDPOINT_CHANGED", "Service endpoint changed during introspection; repeat the import")
    except ConflictError:
        return failure(409, "SCHEMA_IMPORT_CONFLICT", "Concurrent schema import conflicted; retry the import")
    except SchemaCollectionError as exc:
        # Never serialize str(exc), SQL details, response bodies or URLs.
        return failure(exc.status_code, exc.code, exc.public_detail)
    except SQLAlchemyError:
        # The owned session/transaction contexts already rolled back and closed.
        return failure(503, "SCHEMA_STORAGE_UNAVAILABLE", "Schema storage is temporarily unavailable")


@router.get("/schemas", response_model=list[SchemaOut])
async def get_schemas(
    service_id: uuid.UUID = Query(alias="serviceId"),
    session: AsyncSession = Session, principal: Principal = Depends(current_principal),
):
    try:
        await ensure_visible_service(session, service_id, **visibility_kwargs(principal))
    except NotFoundError as exc:
        raise api_errors(exc) from exc
    return await list_schemas(session, service_id)


@router.get("/schemas/{schema_id}/graph", response_model=CoverageGraph)
async def get_schema_graph(
    schema_id: uuid.UUID, session: AsyncSession = Session, principal: Principal = Depends(current_principal)
):
    try:
        await ensure_visible_snapshot(session, schema_id, **visibility_kwargs(principal))
        return await get_graph(session, schema_id)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.post(
    "/runs", status_code=201, response_model=RunOut,
)
async def post_run(payload: RunCreate, session: AsyncSession = Session,
                   principal: Principal = Depends(require_operator)):
    try:
        if not principal.admin:
            await ensure_visible_snapshot(session, payload.snapshot_id, require_grant=True, **visibility_kwargs(principal))
        return await create_run(session, payload.snapshot_id, payload.name, payload.metadata)
    except (NotFoundError, ConflictError) as exc:
        raise api_errors(exc) from exc


@router.get("/runs", response_model=list[RunOut])
async def get_runs(
    snapshot_id: uuid.UUID = Query(alias="snapshotId"),
    session: AsyncSession = Session, principal: Principal = Depends(current_principal),
):
    try:
        await ensure_visible_snapshot(session, snapshot_id, **visibility_kwargs(principal))
    except NotFoundError as exc:
        raise api_errors(exc) from exc
    return await list_runs(session, snapshot_id, include_metadata=principal.admin)


@router.post(
    "/runs/{run_id}/finish", response_model=RunOut,
)
async def post_finish_run(run_id: uuid.UUID, session: AsyncSession = Session,
                          principal: Principal = Depends(require_operator)):
    try:
        if not principal.admin:
            await ensure_visible_run(session, run_id, require_grant=True, **visibility_kwargs(principal))
        return await finish_run(session, run_id)
    except (NotFoundError, ConflictError) as exc:
        raise api_errors(exc) from exc


@router.get("/runs/{run_id}/coverage", response_model=CoverageGraph)
async def get_coverage(
    run_id: uuid.UUID, session: AsyncSession = Session, principal: Principal = Depends(current_principal)
):
    try:
        await ensure_visible_run(session, run_id, **visibility_kwargs(principal))
        return await get_run_graph(session, run_id)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.get(
    "/runs/{run_id}/operations", response_model=list[OperationOut],
    responses={404: {"description": "Run not found or service is private"}},
    description="Aggregated operations for public services, or private services with an authorized session or CLI key. "
                "Groups require a known query hash and operation type; failed parsing is not assigned a synthetic identity.",
)
async def get_operations(
    run_id: uuid.UUID,
    operation_name: str | None = Query(default=None, alias="operationName", description="Exact, case-sensitive name match."),
    operation_type: OperationType | None = Query(default=None, alias="operationType"),
    session: AsyncSession = Session, principal: Principal = Depends(current_principal),
):
    try:
        await ensure_visible_run(session, run_id, **visibility_kwargs(principal))
        return await list_operations(session, run_id, operation_name, operation_type)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.get(
    "/runs/{run_id}/stats", response_model=RunStats,
    responses={404: {"description": "Run not found or service is private"}},
    description="Run statistics using Coverage Engine coverage. Public services allow anonymous reads; "
                "private services require service access. Timeline uses nonempty one-minute UTC intervals, "
                "including all observed requests. Unknown durations are excluded from averages.",
)
async def get_stats(
    run_id: uuid.UUID, session: AsyncSession = Session, principal: Principal = Depends(current_principal),
):
    try:
        await ensure_visible_run(session, run_id, **visibility_kwargs(principal))
        return await get_run_stats(session, run_id)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.get(
    "/runs/{run_id}/requests", response_model=list[RequestOut],
)
async def get_requests(
    run_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Session,
    principal: Principal = Depends(require_analyst),
):
    try:
        await ensure_visible_run(session, run_id, require_grant=True, **visibility_kwargs(principal))
        return await list_requests(session, run_id, limit, offset)
    except NotFoundError as exc:
        raise api_errors(exc) from exc
