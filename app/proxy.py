"""Transparent HTTP proxy with opt-in, synchronous E2E observation capture."""

import asyncio
import time
import uuid
import zlib
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import require_role
from app.auth import Principal, current_principal, visibility_kwargs
from app.config import target_graphql_url
from app.diagnostics import internal_failure
from app.database import SessionFactory, get_session_factory
from app.e2e_capture import capture_e2e_exchange, load_run_snapshot
from app.services import ConflictError, NotFoundError, ensure_visible_run
from app.models import GraphQLService
from app.upstream import (validate_endpoint, UnsafeEndpoint, legacy_enabled, PinnedTransport,
                          MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, UpstreamTooLarge, outbound_slot)


router = APIRouter(tags=["GraphQL Proxy"],
                   responses={401: {"description": "Authentication required"},
                              403: {"description": "Insufficient role or invalid Origin/CSRF"},
                              404: {"description": "Run not found or service access denied"}})
EXCLUDED_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
    "cookie2", "set-cookie2", "host", "content-length", "x-coverage-run-id", "x-coverage-api-key", "x-csrf-token",
}
MAX_ANALYSIS_RESPONSE_BYTES = 1024 * 1024


async def get_proxy_session(request: Request) -> AsyncIterator[AsyncSession | None]:
    # Plain proxy traffic must work even when PostgreSQL is unavailable.
    if "x-coverage-run-id" not in request.headers:
        yield None
        return
    async with SessionFactory() as session:
        yield session


async def proxy_principal(request: Request, sessions=Depends(get_session_factory)) -> Principal:
    # Cookie authentication has its own short read transaction. A request-scoped
    # auth session would otherwise retain a second checkout throughout HTTP.
    async with sessions() as session:
        return await current_principal(request, session)


require_proxy_operator = require_role("operator", principal_dependency=proxy_principal)


def coverage_run_id(request: Request) -> uuid.UUID | None:
    values = request.headers.getlist("x-coverage-run-id")
    if not values:
        return None
    try:
        if len(values) != 1:
            raise ValueError
        # Accept canonical UUID notation only, not UUID URNs, braces or lists.
        value = values[0].strip()
        run_id = uuid.UUID(value)
        if str(run_id) != value.lower():
            raise ValueError
        return run_id
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Coverage-Run-Id must contain one UUID") from None


def decoded_response_copy(content: bytes, headers: httpx.Headers) -> bytes | None:
    """Bound both encoded input and decoded output; never alter wire bytes.

    Only identity, gzip and zlib-wrapped deflate are supported for analysis.
    Unknown/stacked encodings, concatenated streams, invalid/truncated streams
    and bodies over the limit leave response flags unknown.
    """
    if len(content) > MAX_ANALYSIS_RESPONSE_BYTES:
        return None
    encoding = headers.get("content-encoding", "identity").strip().lower()
    if encoding == "identity":
        return content
    if encoding not in {"gzip", "deflate"}:
        return None
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
        decoded = decoder.decompress(content, MAX_ANALYSIS_RESPONSE_BYTES + 1)
        if (len(decoded) > MAX_ANALYSIS_RESPONSE_BYTES or not decoder.eof
                or decoder.unused_data or decoder.unconsumed_tail):
            return None
        return decoded
    except zlib.error:
        return None


def forwarded_headers(
    headers: httpx.Headers, *, strip_forwarding: bool = False, allow_cookies: bool = False,
) -> list[tuple[bytes, bytes]]:
    """Cookies are denied unless the caller selected trusted, non-browser E2E mode."""
    excluded = EXCLUDED_HEADERS | {
        token.strip().lower()
        for value in headers.get_list("connection")
        for token in value.split(",")
    }
    if strip_forwarding:
        excluded.update({"forwarded", "x-real-ip"})
        excluded.update(name.lower() for name in headers if name.lower().startswith("x-forwarded-"))
    output = []
    reserved_cookies = {"__Host-coverage_session", "coverage_dev_session"}
    for name, value in headers.raw:
        lower = name.decode("ascii").lower()
        if (lower in excluded or lower.startswith("x-coverage-")
                or (not allow_cookies and lower in {"cookie", "set-cookie"})):
            continue
        if lower == "cookie":
            value = b"; ".join(part.strip() for part in value.split(b";")
                               if part.strip().split(b"=", 1)[0].decode("latin1") not in reserved_cookies)
            if not value:
                continue
        if lower == "set-cookie" and value.split(b"=", 1)[0].strip().decode("latin1") in reserved_cookies:
            continue
        output.append((name, value))
    return output


def trusted_e2e_cookies(request: Request, principal: Principal) -> bool:
    # API-key authentication is explicit, unlike ambient browser cookies. Even
    # with a valid key, browser-shaped requests never get cookie passthrough.
    # Keep this per-exchange policy when upstream routing becomes multi-service;
    # never introduce a shared cookie jar or a global cookie-name allowlist.
    return principal.api_key and not any(
        name.lower() == "origin" or name.lower().startswith("sec-fetch-")
        for name in request.headers
    )


@router.post("/graphql", responses={
    400: {"description": "Missing, malformed or duplicate run UUID"},
    409: {"description": "Run is not running or service endpoint is missing"},
    413: {"description": "Request exceeds 1 MiB"},
    502: {"description": "Upstream/capture failure; request may have executed, never retry automatically"},
    503: {"description": "Storage, destination policy, DNS or outbound capacity unavailable"},
    504: {"description": "Upstream timeout; request may have executed"},
}, openapi_extra={"parameters": [{"name": "X-Coverage-Run-Id", "in": "header", "required": False,
    "description": "Required UUID of a running run; determines service upstream. Only explicit legacy CLI mode permits omission.",
    "schema": {"type": "string", "format": "uuid"}}]},
    description="Routes through run → snapshot → service.endpointUrl. Original body is forwarded unchanged. "
                "Browser cookies and internal coverage headers are stripped. No redirects or automatic retries.")
async def graphql_proxy(request: Request, session: AsyncSession | None = Depends(get_proxy_session),
                        principal: Principal = Depends(require_proxy_operator)):
    run_id = coverage_run_id(request)
    analyzer = None
    if run_id is None and (not trusted_e2e_cookies(request, principal) or not legacy_enabled()):
        raise HTTPException(status_code=400, detail="X-Coverage-Run-Id is required (CLI compatibility requires explicit legacy mode)")
    target_url = None
    if run_id is not None:
        analyzer = getattr(request.app.state, "coverage_analyzer", None)
        if not callable(analyzer):
            raise HTTPException(status_code=503, detail="Coverage analyzer is unavailable")
        try:
            if not principal.admin:
                await ensure_visible_run(session, run_id, require_grant=True, **visibility_kwargs(principal))
            snapshot = await load_run_snapshot(session, run_id)
            service = await session.get(GraphQLService, snapshot.service_id)
            if service is None:
                raise NotFoundError("GraphQL service not found")
            target_url = service.endpoint_url
            # End the read transaction and release the checkout before DNS,
            # request body reads or upstream HTTP. AsyncSession.close() resets
            # this session; capture below starts a new transaction and still
            # checks/locks the run in record_observation against finish_run.
            await session.close()
        except NotFoundError:
            raise HTTPException(status_code=404, detail="Coverage run or schema snapshot not found") from None
        except ConflictError:
            raise HTTPException(status_code=409, detail="Coverage run is not running") from None
        except Exception as exc:
            internal_failure("coverage_preflight", exc, run_id=run_id)
            raise HTTPException(status_code=503, detail="Coverage storage is unavailable") from None

    if not target_url:
        if not legacy_enabled():
            raise HTTPException(status_code=409, detail="Service endpoint is not configured; set endpointUrl before starting a run")
        target_url = target_graphql_url()
    try:
        destination = await validate_endpoint(target_url)
        target = destination.url
    except UnsafeEndpoint:
        raise HTTPException(status_code=503, detail="GraphQL upstream destination is unavailable or not permitted") from None

    chunks = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(chunks) + len(chunk) > MAX_REQUEST_BYTES:
                    raise HTTPException(status_code=413, detail="GraphQL request exceeds the size limit")
                chunks.extend(chunk)
    except TimeoutError:
        raise HTTPException(status_code=408, detail="GraphQL request body timed out") from None
    body = bytes(chunks)
    allow_cookies = trusted_e2e_cookies(request, principal)
    headers = forwarded_headers(httpx.Headers(request.headers.raw), strip_forwarding=True,
                                allow_cookies=allow_cookies)
    started = time.perf_counter()
    try:
        # No shared cookie jar, socket pool, DNS resolution or automatic retry.
        async with outbound_slot(), asyncio.timeout(30):
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=False,
                                         transport=PinnedTransport(destination)) as client:
                client.headers.clear()
                async with client.stream("POST", target, content=body, headers=headers) as upstream:
                    content_buffer = bytearray()
                    async for chunk in upstream.aiter_raw():
                        if len(content_buffer) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise UpstreamTooLarge()
                        content_buffer.extend(chunk)
                    content = bytes(content_buffer)
                    duration_ms = max(0, int((time.perf_counter() - started) * 1000))
                    response = Response(content=content, status_code=upstream.status_code)
                    response.raw_headers.extend(forwarded_headers(upstream.headers, allow_cookies=allow_cookies))
    except UpstreamTooLarge:
        raise HTTPException(status_code=502, detail="Upstream response exceeds the size limit; upstream may have executed the request. Do not retry automatically.") from None
    except (httpx.TimeoutException, TimeoutError) as exc:
        internal_failure("upstream_request", exc, run_id=run_id)
        raise HTTPException(status_code=504, detail="GraphQL upstream timed out; upstream may have executed the request. Do not retry automatically.") from None
    except httpx.RequestError as exc:
        internal_failure("upstream_request", exc, run_id=run_id)
        raise HTTPException(status_code=502, detail="GraphQL upstream request failed; upstream may have executed the request. Do not retry automatically.") from None

    if run_id is not None:
        try:
            await capture_e2e_exchange(
                session, run_id=run_id, request_body=body,
                decoded_response_body=decoded_response_copy(content, upstream.headers),
                http_status=upstream.status_code, duration_ms=duration_ms, analyzer=analyzer,
            )
        except Exception as exc:
            internal_failure("observation_save", exc, run_id=run_id)
            # The upstream may already have performed a mutation. Do not return
            # success, retry upstream, or expose SQL/analyzer exception details.
            raise HTTPException(
                status_code=502,
                detail="Coverage observation could not be saved; upstream may have executed the request. Do not retry automatically.",
            ) from None
    return response
