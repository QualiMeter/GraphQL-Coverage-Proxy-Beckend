import asyncio
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from anyio import to_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import AwareDatetime, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import defer

from app.access import require_analyst
from app.api_models import ApiModel, ComparedFieldOut, RunComparisonRequest
from app.auth import check_csrf, current_principal, visibility_kwargs
from app.contracts import OperationType, RunStatus
from app.database import get_session_factory
from app.diagnostics import internal_failure
from app.export_data import (
    columns, coverage_export, matrix_export, operations_export, requests_export, tests_export,
)
from app.export_files import BATCH_SIZE, MAX_EXPORT_ROWS, ExportFile, ExportLimitError
from app.journal import RequestFilters
from app.models import GraphQLService, SchemaSnapshot, TestRun
from app.run_comparison import RunComparisonError, compare_run_series, load_run_comparison
from app.services import NotFoundError, ensure_visible_run
from app.test_executions import ExecutionFilters

ExportFormat = Literal["json", "csv"]
PRIVATE = {"security": [{"CoverageApiKey": []}, {"CoverageSession": []}]}
OPTIONAL_AUTH = {"security": [{}, {"CoverageApiKey": []}, {"CoverageSession": []}]}
DOWNLOAD_RESPONSES = {
    200: {"description": "Complete filtered export; attachment, format version 1.",
          "content": {"application/json": {"schema": {"type": "object"}},
                      "text/csv": {"schema": {"type": "string", "format": "binary"}}}},
    404: {"description": "Run not found or inaccessible"},
    401: {"description": "Authentication required for protected exports"},
    403: {"description": "Insufficient role or invalid Origin/CSRF"},
    413: {"description": "Export size limit exceeded; narrow the filters"},
    503: {"description": "Export generation timed out or storage unavailable"},
}
router = APIRouter(prefix="/api/runs", tags=["Exports"], responses=DOWNLOAD_RESPONSES)


class ExportService(ApiModel):
    id: uuid.UUID
    key: str
    name: str


class ExportSnapshot(ApiModel):
    id: uuid.UUID
    schema_hash: str


class ExportRun(ApiModel):
    id: uuid.UUID
    name: str
    status: RunStatus
    started_at: AwareDatetime
    finished_at: AwareDatetime | None


class ExportMetadata(ApiModel):
    generated_at: AwareDatetime
    service: ExportService
    snapshot: ExportSnapshot
    runs: list[ExportRun]


class ExportDocument(ApiModel):
    format_version: Literal[1]
    dataset: Literal["coverage", "operations", "requests", "tests", "test-matrix", "comparison"]
    metadata: ExportMetadata
    filters: dict[str, Any]
    summary: dict[str, Any]
    row_count: int = Field(ge=0, le=100_000)
    rows: list[dict[str, Any]]


class CoverageExportFilters(ApiModel):
    model_config = ConfigDict(**{**ApiModel.model_config, "extra": "forbid"})
    format: ExportFormat = "json"
    type_name: str | None = Field(default=None, min_length=1, max_length=255)
    field_name: str | None = Field(default=None, min_length=1, max_length=255)
    covered: bool | None = None
    search: str | None = Field(default=None, max_length=1000)
    min_hits: int | None = Field(default=None, ge=0, lt=2**63)
    max_hits: int | None = Field(default=None, ge=0, lt=2**63)
    returns: Literal["scalar", "object", "list"] | None = None
    deprecated: bool | None = None
    sort_by: Literal["coordinate", "fieldName", "hits"] = "coordinate"
    sort_order: Literal["asc", "desc"] = "asc"

    @model_validator(mode="after")
    def field_scope(self):
        if self.field_name is not None and self.type_name is None:
            raise ValueError("fieldName requires typeName")
        if self.min_hits is not None and self.max_hits is not None and self.min_hits > self.max_hits:
            raise ValueError("minHits must not exceed maxHits")
        return self


class RequestExportFilters(RequestFilters):
    model_config = ConfigDict(**{**RequestFilters.model_config, "extra": "forbid"})
    format: ExportFormat = "json"
    error_group: Literal["http", "analyzer"] | None = None


class TestExportFilters(ExecutionFilters):
    model_config = ConfigDict(**{**ExecutionFilters.model_config, "extra": "forbid"})
    format: ExportFormat = "json"


class OperationExportFilters(ApiModel):
    model_config = ConfigDict(**{**ApiModel.model_config, "extra": "forbid"})
    format: ExportFormat = "json"
    operation_name: str | None = None
    operation_type: OperationType | None = None
    search: str | None = Field(default=None, max_length=1000)
    type_name: str | None = Field(default=None, min_length=1, max_length=255)
    field_name: str | None = Field(default=None, min_length=1, max_length=255)
    has_errors: bool | None = None
    sort_by: Literal["identity", "count", "duration", "errors"] = "identity"
    sort_order: Literal["asc", "desc"] = "asc"

    @model_validator(mode="after")
    def field_scope(self):
        if self.field_name is not None and self.type_name is None:
            raise ValueError("fieldName requires typeName")
        return self


async def metadata_for_run(session, run):
    row = (await session.execute(select(
        GraphQLService.id, GraphQLService.key, GraphQLService.name, SchemaSnapshot.schema_hash,
    ).join(SchemaSnapshot, SchemaSnapshot.service_id == GraphQLService.id)
        .where(SchemaSnapshot.id == run.snapshot_id))).one()
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "service": {"id": str(row.id), "key": row.key, "name": row.name},
        "snapshot": {"id": str(run.snapshot_id), "schemaHash": row.schema_hash},
        "runs": [{"id": str(run.id), "name": run.name, "status": run.status,
                  "startedAt": run.started_at.astimezone(timezone.utc).isoformat(),
                  "finishedAt": run.finished_at.astimezone(timezone.utc).isoformat() if run.finished_at else None}],
    }


def export_error(exc):
    if isinstance(exc, NotFoundError):
        return HTTPException(404, "Test run not found")
    if isinstance(exc, ExportLimitError) or isinstance(exc, RunComparisonError) and exc.code == "EXPORT_TOO_LARGE":
        return HTTPException(413, {"code": "export_too_large", "message": str(exc)})
    if isinstance(exc, RunComparisonError):
        return HTTPException(409, {"code": exc.code, "message": exc.detail})
    internal_failure("export_generation", exc)
    return HTTPException(503, "Export generation is temporarily unavailable")


async def run_file(request, sessions, run_id, filters, build, *, restricted=False):
    writer = ExportFile(filters.format)
    try:
        async with asyncio.timeout(60):
            async with sessions() as session:
                async with session.begin():
                    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                    await session.execute(text("SET LOCAL statement_timeout = '30s'"))
                    principal = await current_principal(request, session)
                    if restricted:
                        await require_analyst(request, principal=principal)
                    await ensure_visible_run(session, run_id, require_grant=restricted, **visibility_kwargs(principal))
                    run = await session.get(TestRun, run_id, options=[defer(TestRun.metadata_json)])
                    metadata = await metadata_for_run(session, run)
                    await build(session, run, filters, writer, metadata)
            await to_thread.run_sync(writer.finish)
        return writer.response(run_id)
    except (NotFoundError, ExportLimitError, SQLAlchemyError, TimeoutError, OSError) as exc:
        writer.close()
        raise export_error(exc) from None
    except BaseException:
        writer.close()
        raise


@router.get("/{run_id}/exports/coverage", response_model=ExportDocument, openapi_extra=OPTIONAL_AUTH)
async def export_coverage(run_id: uuid.UUID, request: Request,
    filters: Annotated[CoverageExportFilters, Query()], sessions=Depends(get_session_factory)):
    return await run_file(request, sessions, run_id, filters, coverage_export)


@router.get("/{run_id}/exports/operations", response_model=ExportDocument, openapi_extra=OPTIONAL_AUTH)
async def export_operations(run_id: uuid.UUID, request: Request,
    filters: Annotated[OperationExportFilters, Query()], sessions=Depends(get_session_factory)):
    return await run_file(request, sessions, run_id, filters, operations_export)


@router.get("/{run_id}/exports/requests", response_model=ExportDocument, openapi_extra=PRIVATE)
async def export_requests(run_id: uuid.UUID, request: Request,
    filters: Annotated[RequestExportFilters, Query()], sessions=Depends(get_session_factory)):
    return await run_file(request, sessions, run_id, filters, requests_export, restricted=True)


@router.get("/{run_id}/exports/tests", response_model=ExportDocument, openapi_extra=PRIVATE)
async def export_tests(run_id: uuid.UUID, request: Request,
    filters: Annotated[TestExportFilters, Query()], sessions=Depends(get_session_factory)):
    return await run_file(request, sessions, run_id, filters, tests_export, restricted=True)


@router.get("/{run_id}/exports/test-matrix", response_model=ExportDocument, openapi_extra=PRIVATE)
async def export_matrix(run_id: uuid.UUID, request: Request,
    filters: Annotated[TestExportFilters, Query()], sessions=Depends(get_session_factory)):
    return await run_file(request, sessions, run_id, filters, matrix_export, restricted=True)


@router.post("/compare/export", response_model=ExportDocument,
             openapi_extra=OPTIONAL_AUTH,
             responses={409: {"description": "Runs must be completed and share one snapshot"}})
async def export_comparison(payload: RunComparisonRequest, request: Request,
    format: ExportFormat = "json", sessions=Depends(get_session_factory),
    _csrf: Annotated[str | None, Header(alias="X-CSRF-Token", description="Required with a browser session.")] = None):
    writer = ExportFile(format)
    try:
        async with asyncio.timeout(60):
            async with sessions() as session:
                async with session.begin():
                    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                    await session.execute(text("SET LOCAL statement_timeout = '30s'"))
                    principal = await current_principal(request, session)
                    if principal.token is not None:
                        check_csrf(request, principal)
                    data = await load_run_comparison(session, payload, principal,
                                                     max_fields=MAX_EXPORT_ROWS // len(payload.run_ids))
                    run = await session.get(TestRun, payload.baseline_run_id, options=[defer(TestRun.metadata_json)])
                    metadata = await metadata_for_run(session, run)
            selection = payload.model_copy(update={
                "fields": payload.fields.model_copy(update={"offset": 0, "limit": MAX_EXPORT_ROWS}),
                "types": payload.types.model_copy(update={"offset": 0, "limit": MAX_EXPORT_ROWS}),
            })
            result = await compare_run_series(data, selection)
            metadata["runs"] = [{"id": str(row.run_id), "name": row.name, "status": row.status,
                                  "startedAt": row.started_at.isoformat(),
                                  "finishedAt": row.finished_at.isoformat() if row.finished_at else None}
                                 for row in result.runs]
            summary = await to_thread.run_sync(lambda: result.model_dump(
                mode="json", by_alias=True, exclude={"fields", "generated_at"}))
            await writer.start("comparison", metadata, {"fieldFilter": payload.fields.filter}, summary,
                               columns(ComparedFieldOut), result.fields.total)
            for offset in range(0, len(result.fields.items), BATCH_SIZE):
                await writer.add_batch(result.fields.items[offset:offset + BATCH_SIZE])
            await to_thread.run_sync(writer.finish)
        return writer.response(payload.baseline_run_id)
    except (NotFoundError, ExportLimitError, RunComparisonError, SQLAlchemyError, TimeoutError, OSError) as exc:
        writer.close()
        raise export_error(exc) from None
    except BaseException:
        writer.close()
        raise
