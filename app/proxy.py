"""Transparent HTTP proxy with opt-in, synchronous E2E observation capture."""

import time
import uuid
import zlib
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import require_admin
from app.config import target_graphql_url
from app.database import SessionFactory
from app.e2e_capture import capture_e2e_exchange, load_run_snapshot
from app.services import ConflictError, NotFoundError


router = APIRouter(tags=["GraphQL Proxy"])
EXCLUDED_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
    "host", "content-length", "x-coverage-run-id", "x-coverage-api-key",
}
MAX_ANALYSIS_RESPONSE_BYTES = 1024 * 1024


async def get_proxy_session(request: Request) -> AsyncIterator[AsyncSession | None]:
    # Plain proxy traffic must work even when PostgreSQL is unavailable.
    if "x-coverage-run-id" not in request.headers:
        yield None
        return
    async with SessionFactory() as session:
        yield session


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


def forwarded_headers(headers: httpx.Headers, *, strip_forwarding: bool = False) -> list[tuple[bytes, bytes]]:
    excluded = EXCLUDED_HEADERS | {
        token.strip().lower()
        for value in headers.get_list("connection")
        for token in value.split(",")
    }
    if strip_forwarding:
        excluded.update({"forwarded", "x-real-ip"})
        excluded.update(name.lower() for name in headers if name.lower().startswith("x-forwarded-"))
    return [(name, value) for name, value in headers.raw if name.decode("ascii").lower() not in excluded]


@router.post("/graphql", dependencies=[Depends(require_admin)])
async def graphql_proxy(request: Request, session: AsyncSession | None = Depends(get_proxy_session)):
    run_id = coverage_run_id(request)
    analyzer = None
    if run_id is not None:
        analyzer = getattr(request.app.state, "coverage_analyzer", None)
        if not callable(analyzer):
            raise HTTPException(status_code=503, detail="Coverage analyzer is unavailable")
        try:
            await load_run_snapshot(session, run_id)
        except NotFoundError:
            raise HTTPException(status_code=404, detail="Coverage run or schema snapshot not found") from None
        except ConflictError:
            raise HTTPException(status_code=409, detail="Coverage run is not running") from None
        except Exception:
            raise HTTPException(status_code=503, detail="Coverage storage is unavailable") from None

    try:
        target_url = target_graphql_url()
        target = httpx.URL(target_url)
        # HTTPX normalizes away empty userinfo; inspect the original authority too.
        has_userinfo = "@" in urlsplit(target_url).netloc
    except (httpx.InvalidURL, ValueError):
        raise HTTPException(status_code=503, detail="TARGET_GRAPHQL_URL is not configured correctly") from None
    if target.scheme not in {"http", "https"} or not target.host or has_userinfo:
        raise HTTPException(status_code=503, detail="TARGET_GRAPHQL_URL is not configured correctly")

    body = await request.body()
    headers = forwarded_headers(httpx.Headers(request.headers.raw), strip_forwarding=True)
    started = time.perf_counter()
    try:
        # A separate cookie jar per request prevents cross-client session leakage.
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=False) as client:
            client.headers.clear()
            async with client.stream("POST", target, content=body, headers=headers) as upstream:
                # Raw bytes retain Content-Encoding, even for encodings httpx cannot decode.
                content = b"".join([chunk async for chunk in upstream.aiter_raw()])
                duration_ms = max(0, int((time.perf_counter() - started) * 1000))
                response = Response(content=content, status_code=upstream.status_code)
                response.raw_headers.extend(forwarded_headers(upstream.headers))
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="GraphQL upstream timed out") from None
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="GraphQL upstream request failed") from None

    if run_id is not None:
        try:
            await capture_e2e_exchange(
                session, run_id=run_id, request_body=body,
                decoded_response_body=decoded_response_copy(content, upstream.headers),
                http_status=upstream.status_code, duration_ms=duration_ms, analyzer=analyzer,
            )
        except Exception:
            # The upstream may already have performed a mutation. Do not return
            # success, retry upstream, or expose SQL/analyzer exception details.
            raise HTTPException(
                status_code=502,
                detail="Coverage observation could not be saved; upstream may have executed the request. Do not retry automatically.",
            ) from None
    return response
