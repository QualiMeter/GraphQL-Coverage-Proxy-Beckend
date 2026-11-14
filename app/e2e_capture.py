"""In-memory GraphQL exchange processing for the instrumented proxy.

The analyzer is injected explicitly. This module never invents covered fields
and never persists GraphQL documents, variables, response bodies or credentials.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import AnalysisResult, GraphQLQueryInput, RequestObservationInput
from app.models import SchemaSnapshot, TestRun
from app.services import ConflictError, NotFoundError, record_observation


Analyzer = Callable[[GraphQLQueryInput, str], Awaitable[AnalysisResult]]


def parse_graphql_request(body: bytes) -> GraphQLQueryInput | None:
    """Decode a single standard GraphQL-over-HTTP JSON request, if supported."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None

    if not isinstance(payload, dict):
        return None
    query = payload.get("query")
    operation_name = payload.get("operationName")
    variables = payload.get("variables")
    if not isinstance(query, str) or not query.strip():
        return None
    if operation_name is not None and not isinstance(operation_name, str):
        return None
    if variables is None:
        variables = {}
    if not isinstance(variables, dict):
        return None
    return GraphQLQueryInput(
        query=query,
        operation_name=operation_name,
        variables=variables,
    )


def graphql_response_flags(decoded_body: bytes | None) -> tuple[bool | None, bool | None]:
    """Read GraphQL envelope flags without asserting any resolver succeeded.

    The caller passes a *decoded* response body. For compressed responses,
    network failures, or non-JSON responses it can pass None to avoid guessing.
    """
    if decoded_body is None:
        return None, None
    try:
        payload = json.loads(decoded_body)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    # `data: null` is not evidence that any selected field produced usable data.
    # A partial object with errors may still count selected (not necessarily
    # successfully resolved) fields; the dashboard describes that limitation.
    return isinstance(payload.get("data"), dict), bool(payload.get("errors"))


async def load_run_snapshot(session: AsyncSession, run_id: uuid.UUID) -> SchemaSnapshot:
    """Resolve only the specified running run and its immutable schema snapshot."""
    run = await session.get(TestRun, run_id, populate_existing=True)
    if run is None:
        raise NotFoundError("Test run not found")
    if run.status != "running":
        raise ConflictError("Cannot record observations for a finished run")
    snapshot = await session.get(SchemaSnapshot, run.snapshot_id)
    if snapshot is None:
        raise NotFoundError("Schema snapshot not found")
    return snapshot


async def capture_e2e_exchange(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    request_body: bytes,
    decoded_response_body: bytes | None,
    http_status: int | None,
    duration_ms: int | None,
    analyzer: Analyzer,
    http_method: str = "POST",
) -> uuid.UUID:
    """Analyze one completed upstream exchange and commit its observation.

    The proxy awaits this commit before delivering the upstream response.
    This function receives in-memory copies solely for analysis.
    """
    snapshot = await load_run_snapshot(session, run_id)

    gql_input = parse_graphql_request(request_body)
    query_hash: str | None = None
    if gql_input is None:
        analysis = AnalysisResult(
            status="unsupported", error_code="unsupported_request_body"
        )
    else:
        query_hash = hashlib.sha256(gql_input.query.encode("utf-8")).hexdigest()
        try:
            analysis = await analyzer(gql_input, snapshot.schema_sdl)
        except Exception:
            # An analyzer failure must not create fake coverage or persist
            # exception messages that may contain GraphQL variables.
            analysis = AnalysisResult(
                status="internal_error", error_code="analyzer_failure"
            )

    response_has_data, graphql_has_errors = graphql_response_flags(decoded_response_body)
    return await record_observation(
        session,
        RequestObservationInput(
            run_id=run_id,
            analysis=analysis,
            http_method=http_method,
            http_status=http_status,
            response_has_data=response_has_data,
            graphql_has_errors=graphql_has_errors,
            duration_ms=duration_ms,
            query_hash=query_hash,
        ),
    )
