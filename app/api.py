"""Frontend-facing REST endpoints. Actual proxy /graphql belongs to teammate #1."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from graphql import GraphQLError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_models import (
    CoverageGraph, RequestOut, RunCreate, RunOut, SchemaImport, SchemaIntrospectionImport,
    SchemaOut, ServiceCreate, ServiceOut, ServiceVisibilityUpdate,
    OperationOut, OperationType, RunStats,
)
from app.access import is_admin, require_admin
from app.config import target_graphql_url
from app.database import get_session
from app.run_analytics import get_run_stats, list_operations
from app.schema_collector import (
    InvalidIntrospectionResponseError, InvalidSchemaTargetURLError, SchemaConnectionError,
)
from app.services import (
    ConflictError, NotFoundError, change_service_visibility, create_run, create_service,
    ensure_visible_run, ensure_visible_service, ensure_visible_snapshot, finish_run,
    get_graph, get_run_graph, import_introspection_schema, import_sdl,
    list_requests, list_runs, list_schemas, list_services,
)


router = APIRouter(prefix="/api", tags=["Coverage API"])
Session = Depends(get_session)


def api_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.post(
    "/services", status_code=201, response_model=ServiceOut,
    dependencies=[Depends(require_admin)],
)
async def post_service(payload: ServiceCreate, session: AsyncSession = Session):
    try:
        return await create_service(session, payload.key, payload.name, payload.is_public)
    except ConflictError as exc:
        raise api_errors(exc) from exc


@router.get("/services", response_model=list[ServiceOut])
async def get_services(session: AsyncSession = Session, admin: bool = Depends(is_admin)):
    return await list_services(session, public_only=not admin)


@router.patch(
    "/services/{service_id}", response_model=ServiceOut,
    dependencies=[Depends(require_admin)],
)
async def patch_service_visibility(
    service_id: uuid.UUID, payload: ServiceVisibilityUpdate, session: AsyncSession = Session
):
    try:
        return await change_service_visibility(session, service_id, payload.is_public)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.post(
    "/schemas/import", status_code=201, response_model=SchemaOut,
    dependencies=[Depends(require_admin)],
)
async def post_schema(payload: SchemaImport, session: AsyncSession = Session):
    try:
        return await import_sdl(session, payload.service_id, payload.sdl)
    except (NotFoundError, ConflictError, ValueError, GraphQLError) as exc:
        raise api_errors(exc) from exc


@router.post(
    "/schemas/import/introspection", status_code=201, response_model=SchemaOut,
    dependencies=[Depends(require_admin)],
)
async def post_introspection_schema(
    payload: SchemaIntrospectionImport, session: AsyncSession = Session
):
    # Clients cannot select an arbitrary target; the same configured upstream
    # is used for the /graphql proxy and schema introspection.
    target_url = target_graphql_url()
    if not target_url:
        raise HTTPException(status_code=503, detail="TARGET_GRAPHQL_URL is not configured")
    try:
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
    session: AsyncSession = Session, admin: bool = Depends(is_admin),
):
    try:
        await ensure_visible_service(session, service_id, admin=admin)
    except NotFoundError as exc:
        raise api_errors(exc) from exc
    return await list_schemas(session, service_id)


@router.get("/schemas/{schema_id}/graph", response_model=CoverageGraph)
async def get_schema_graph(
    schema_id: uuid.UUID, session: AsyncSession = Session, admin: bool = Depends(is_admin)
):
    try:
        await ensure_visible_snapshot(session, schema_id, admin=admin)
        return await get_graph(session, schema_id)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.post(
    "/runs", status_code=201, response_model=RunOut,
    dependencies=[Depends(require_admin)],
)
async def post_run(payload: RunCreate, session: AsyncSession = Session):
    try:
        return await create_run(session, payload.snapshot_id, payload.name, payload.metadata)
    except (NotFoundError, ConflictError) as exc:
        raise api_errors(exc) from exc


@router.get("/runs", response_model=list[RunOut])
async def get_runs(
    snapshot_id: uuid.UUID = Query(alias="snapshotId"),
    session: AsyncSession = Session, admin: bool = Depends(is_admin),
):
    try:
        await ensure_visible_snapshot(session, snapshot_id, admin=admin)
    except NotFoundError as exc:
        raise api_errors(exc) from exc
    return await list_runs(session, snapshot_id, include_metadata=admin)


@router.post(
    "/runs/{run_id}/finish", response_model=RunOut,
    dependencies=[Depends(require_admin)],
)
async def post_finish_run(run_id: uuid.UUID, session: AsyncSession = Session):
    try:
        return await finish_run(session, run_id)
    except (NotFoundError, ConflictError) as exc:
        raise api_errors(exc) from exc


@router.get("/runs/{run_id}/coverage", response_model=CoverageGraph)
async def get_coverage(
    run_id: uuid.UUID, session: AsyncSession = Session, admin: bool = Depends(is_admin)
):
    try:
        await ensure_visible_run(session, run_id, admin=admin)
        return await get_run_graph(session, run_id)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.get(
    "/runs/{run_id}/operations", response_model=list[OperationOut],
    responses={404: {"description": "Run not found or service is private"}},
    description="Aggregated operations for public services, or private services with a valid admin key. "
                "Groups require a known query hash and operation type; failed parsing is not assigned a synthetic identity.",
)
async def get_operations(
    run_id: uuid.UUID,
    operation_name: str | None = Query(default=None, alias="operationName", description="Exact, case-sensitive name match."),
    operation_type: OperationType | None = Query(default=None, alias="operationType"),
    session: AsyncSession = Session, admin: bool = Depends(is_admin),
):
    try:
        await ensure_visible_run(session, run_id, admin=admin)
        return await list_operations(session, run_id, operation_name, operation_type)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.get(
    "/runs/{run_id}/stats", response_model=RunStats,
    responses={404: {"description": "Run not found or service is private"}},
    description="Run statistics using Coverage Engine coverage. Public services allow anonymous reads; "
                "private services require a valid admin key. Timeline uses nonempty one-minute UTC intervals, "
                "including all observed requests. Unknown durations are excluded from averages.",
)
async def get_stats(
    run_id: uuid.UUID, session: AsyncSession = Session, admin: bool = Depends(is_admin),
):
    try:
        await ensure_visible_run(session, run_id, admin=admin)
        return await get_run_stats(session, run_id)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.get(
    "/runs/{run_id}/requests", response_model=list[RequestOut],
    dependencies=[Depends(require_admin)],
)
async def get_requests(
    run_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Session,
):
    try:
        return await list_requests(session, run_id, limit, offset)
    except NotFoundError as exc:
        raise api_errors(exc) from exc
