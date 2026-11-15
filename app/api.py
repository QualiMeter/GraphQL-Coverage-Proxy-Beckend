"""Frontend-facing REST endpoints with role and service access checks."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from graphql import GraphQLError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_models import (
    CoverageGraph, RequestOut, RunCreate, RunOut, SchemaImport, SchemaIntrospectionImport,
    SchemaOut, ServiceCreate, ServiceOut, ServiceUpdate,
    OperationOut, OperationType, RunStats,
)
from app.access import require_operator, require_analyst
from app.auth import Principal, current_principal, visibility_kwargs
from app.config import target_graphql_url
from app.upstream import UnsafeEndpoint, validate_endpoint, legacy_enabled
from app.database import get_session
from app.run_analytics import get_run_stats, list_operations
from app.schema_collector import (
    InvalidIntrospectionResponseError, InvalidSchemaTargetURLError, SchemaConnectionError,
)
from app.services import (
    ConflictError, NotFoundError, update_service, get_service, create_run, create_service,
    ensure_visible_run, ensure_visible_service, ensure_visible_snapshot, finish_run,
    get_graph, get_run_graph, import_introspection_schema, import_sdl,
    list_requests, list_runs, list_schemas, list_services,
)


router = APIRouter(prefix="/api", tags=["Coverage API"],
                   responses={401: {"description": "Authentication required for protected operations"},
                              403: {"description": "Insufficient role or invalid Origin/CSRF"}})
Session = Depends(get_session)


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
)
async def post_introspection_schema(
    payload: SchemaIntrospectionImport, session: AsyncSession = Session,
    principal: Principal = Depends(require_operator),
):
    # Do not import a global upstream schema into a configured service. Service
    # introspection is stage 3; stage 2 supports SDL and explicit legacy only.
    try:
        if not principal.admin:
            await ensure_visible_service(session, payload.service_id, require_grant=True, **visibility_kwargs(principal))
        service = await get_service(session, payload.service_id)
        if service.endpoint_url or not legacy_enabled():
            raise HTTPException(status_code=409, detail="Use SDL import for configured services until service introspection is available")
        target_url = target_graphql_url()
        if not target_url:
            raise HTTPException(status_code=503, detail="TARGET_GRAPHQL_URL is not configured")
        return await import_introspection_schema(session, payload.service_id, target_url)
    except NotFoundError as exc:
        raise api_errors(exc) from exc
    except InvalidSchemaTargetURLError:
        raise HTTPException(
            status_code=503, detail="TARGET_GRAPHQL_URL is not configured correctly"
        ) from None
    except (SchemaConnectionError, InvalidIntrospectionResponseError):
        # Do not expose upstream response bodies, GraphQL errors or URLs.
        raise HTTPException(
            status_code=502, detail="GraphQL schema introspection failed"
        ) from None


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
