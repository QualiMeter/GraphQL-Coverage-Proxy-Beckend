import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import require_analyst, require_operator
from app.auth import Principal, visibility_kwargs
from app.database import get_session
from app.services import ConflictError, NotFoundError, ensure_visible_run
from app.test_executions import (
    ExecutionBusy, ExecutionCreate, ExecutionFieldOut, ExecutionFilters, ExecutionFinish, ExecutionOut,
    TestCoverageSummary, coverage_summary, create_execution, execution_detail, execution_fields,
    execution_page, finish_execution,
)

router = APIRouter(prefix="/api/runs", tags=["Test Coverage"], responses={
    401: {"description": "Authentication required"}, 403: {"description": "Role, grant or CSRF check failed"},
    404: {"description": "Run or execution not found or inaccessible"},
    409: {"description": "Attempt conflict, final result conflict or requests still in flight"},
})


async def authorized(session, run_id, principal):
    await ensure_visible_run(session, run_id, require_grant=True, **visibility_kwargs(principal))


def api_error(exc):
    if isinstance(exc, ExecutionBusy):
        return HTTPException(409, {"code": "test_requests_in_flight", "message": str(exc)})
    return HTTPException(404 if isinstance(exc, NotFoundError) else 409, str(exc))


@router.post("/{run_id}/tests", status_code=201, response_model=ExecutionOut)
async def start_test(run_id: uuid.UUID, payload: ExecutionCreate, session: AsyncSession = Depends(get_session),
                     principal: Principal = Depends(require_operator)):
    try:
        await authorized(session, run_id, principal)
        return await create_execution(session, run_id, payload)
    except (ConflictError, NotFoundError) as exc:
        raise api_error(exc) from None


@router.post("/{run_id}/tests/{execution_id}/finish", response_model=ExecutionOut)
async def finish_test(run_id: uuid.UUID, execution_id: uuid.UUID, payload: ExecutionFinish,
                      session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_operator)):
    try:
        await authorized(session, run_id, principal)
        return await finish_execution(session, run_id, execution_id, payload)
    except (ConflictError, NotFoundError) as exc:
        raise api_error(exc) from None


@router.get("/{run_id}/tests", response_model=list[ExecutionOut])
async def list_tests(run_id: uuid.UUID, response: Response, filters: Annotated[ExecutionFilters, Query()],
                     session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_analyst)):
    try:
        await authorized(session, run_id, principal)
        rows, total = await execution_page(session, run_id, filters)
        response.headers["X-Total-Count"] = str(total)
        return rows
    except NotFoundError as exc:
        raise api_error(exc) from None


@router.get("/{run_id}/tests/{execution_id}", response_model=ExecutionOut)
async def test_detail(run_id: uuid.UUID, execution_id: uuid.UUID, session: AsyncSession = Depends(get_session),
                      principal: Principal = Depends(require_analyst)):
    try:
        await authorized(session, run_id, principal)
        return await execution_detail(session, run_id, execution_id)
    except NotFoundError as exc:
        raise api_error(exc) from None


@router.get("/{run_id}/tests/{execution_id}/fields", response_model=list[ExecutionFieldOut])
async def test_fields(run_id: uuid.UUID, execution_id: uuid.UUID, response: Response,
                      limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0),
                      session: AsyncSession = Depends(get_session), principal: Principal = Depends(require_analyst)):
    try:
        await authorized(session, run_id, principal)
        rows, total = await execution_fields(session, run_id, execution_id, limit, offset)
        response.headers["X-Total-Count"] = str(total)
        return rows
    except NotFoundError as exc:
        raise api_error(exc) from None


@router.get("/{run_id}/test-coverage", response_model=TestCoverageSummary)
async def test_coverage(run_id: uuid.UUID, session: AsyncSession = Depends(get_session),
                        principal: Principal = Depends(require_analyst)):
    try:
        await authorized(session, run_id, principal)
        return await coverage_summary(session, run_id)
    except NotFoundError as exc:
        raise api_error(exc) from None
