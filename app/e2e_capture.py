"""Capture a completed GraphQL exchange without persisting raw bodies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.graphql_errors import ErrorSummary, error_count, response_envelope, sanitize_errors
from app.schema_work import schema_work
from app.field_evidence import match_field_evidence
from app.diagnostics import diagnostic_run, internal_failure
from app.contracts import AnalysisResult, GraphQLQueryInput, RequestObservationInput
from app.safe_document import SafeDocument, normalize_document
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
    _, has_data, has_errors = response_envelope(decoded_body)
    return has_data, has_errors


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
    safe_document = SafeDocument()
    if gql_input is None:
        analysis = AnalysisResult(
            status="unsupported", error_code="unsupported_request_body"
        )
    else:
        try:
            query_hash = hashlib.sha256(gql_input.query.encode("utf-8")).hexdigest()
        except UnicodeError:
            # A JSON string may contain an unpaired surrogate, which has no
            # UTF-8 query hash. Keep unknown identity instead of losing the observation.
            query_hash = None
        with diagnostic_run(run_id):
            try:
                safe_document = await asyncio.to_thread(normalize_document, gql_input.query)
            except Exception as exc:
                internal_failure("safe_document", exc)
                # Optional safe representation must never invalidate an executed mutation.
                safe_document = SafeDocument()
            try:
                analysis = await analyzer(gql_input, snapshot.schema_sdl)
            except Exception as exc:
                internal_failure("analyzer", exc)
                # An analyzer failure must not create fake coverage or persist
                # exception messages that may contain GraphQL variables.
                analysis = AnalysisResult(
                    status="internal_error", error_code="analyzer_failure"
                )

    # Keep established flags/count even if optional sanitization fails.
    payload, response_has_data, graphql_has_errors = await schema_work(
        response_envelope, decoded_response_body,
    )
    summary = ErrorSummary()
    try:
        summary = ErrorSummary(count=await schema_work(error_count, payload))
        summary = await schema_work(
            sanitize_errors, payload, gql_input.query if gql_input else None,
            gql_input.operation_name if gql_input else None, snapshot.schema_sdl,
        )
    except Exception:
        # Never emit upstream exceptions; CancelledError still propagates.
        pass
    evidence = await schema_work(match_field_evidence, analysis, payload)
    return await record_observation(
        session,
        RequestObservationInput(
            run_id=run_id,
            analysis=analysis,
            http_method=http_method,
            http_status=http_status,
            response_has_data=response_has_data,
            graphql_has_errors=graphql_has_errors,
            graphql_errors=summary,
            field_evidence=evidence,
            duration_ms=duration_ms,
            query_hash=query_hash,
            safe_document=safe_document,
        ),
    )
