"""Frontend-facing REST endpoints. Actual proxy /graphql belongs to teammate #1."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from graphql import GraphQLError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_models import (
    CoverageGraph, RequestOut, RunCreate, RunOut, SchemaImport, SchemaOut,
    ServiceCreate, ServiceOut,
)
from app.database import get_session
from app.services import (
    ConflictError, NotFoundError, create_run, create_service, finish_run,
    get_graph, get_run_graph, import_sdl, list_requests, list_runs,
    list_schemas, list_services,
)


router = APIRouter(prefix="/api", tags=["Coverage API"])
Session = Depends(get_session)


def api_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.post("/services", status_code=201, response_model=ServiceOut)
async def post_service(payload: ServiceCreate, session: AsyncSession = Session):
    try:
        return await create_service(session, payload.key, payload.name)
    except ConflictError as exc:
        raise api_errors(exc) from exc


@router.get("/services", response_model=list[ServiceOut])
async def get_services(session: AsyncSession = Session):
    return await list_services(session)


@router.post("/schemas/import", status_code=201, response_model=SchemaOut)
async def post_schema(payload: SchemaImport, session: AsyncSession = Session):
    try:
        return await import_sdl(session, payload.service_id, payload.sdl)
    except (NotFoundError, ConflictError, ValueError, GraphQLError) as exc:
        raise api_errors(exc) from exc


@router.get("/schemas", response_model=list[SchemaOut])
async def get_schemas(service_id: uuid.UUID = Query(alias="serviceId"), session: AsyncSession = Session):
    return await list_schemas(session, service_id)


@router.get("/schemas/{schema_id}/graph", response_model=CoverageGraph)
async def get_schema_graph(schema_id: uuid.UUID, session: AsyncSession = Session):
    try:
        return await get_graph(session, schema_id)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.post("/runs", status_code=201, response_model=RunOut)
async def post_run(payload: RunCreate, session: AsyncSession = Session):
    try:
        return await create_run(session, payload.snapshot_id, payload.name, payload.metadata)
    except (NotFoundError, ConflictError) as exc:
        raise api_errors(exc) from exc


@router.get("/runs", response_model=list[RunOut])
async def get_runs(snapshot_id: uuid.UUID = Query(alias="snapshotId"), session: AsyncSession = Session):
    return await list_runs(session, snapshot_id)


@router.post("/runs/{run_id}/finish", response_model=RunOut)
async def post_finish_run(run_id: uuid.UUID, session: AsyncSession = Session):
    try:
        return await finish_run(session, run_id)
    except (NotFoundError, ConflictError) as exc:
        raise api_errors(exc) from exc


@router.get("/runs/{run_id}/coverage", response_model=CoverageGraph)
async def get_coverage(run_id: uuid.UUID, session: AsyncSession = Session):
    try:
        return await get_run_graph(session, run_id)
    except NotFoundError as exc:
        raise api_errors(exc) from exc


@router.get("/runs/{run_id}/requests", response_model=list[RequestOut])
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
