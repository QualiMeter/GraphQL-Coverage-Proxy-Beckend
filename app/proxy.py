"""HTTP-only GraphQL proxy; request contents are never parsed or persisted."""

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from app.config import target_graphql_url


router = APIRouter(tags=["GraphQL Proxy"])
EXCLUDED_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
    "host", "content-length", "x-coverage-run-id",
}


def forwarded_headers(headers: httpx.Headers) -> list[tuple[bytes, bytes]]:
    excluded = EXCLUDED_HEADERS | {
        token.strip().lower()
        for value in headers.get_list("connection")
        for token in value.split(",")
    }
    return [(name, value) for name, value in headers.raw if name.decode("ascii").lower() not in excluded]


@router.post("/graphql")
async def graphql_proxy(request: Request):
    try:
        target = httpx.URL(target_graphql_url())
    except httpx.InvalidURL:
        raise HTTPException(status_code=503, detail="TARGET_GRAPHQL_URL is not configured correctly") from None
    if target.scheme not in {"http", "https"} or not target.host:
        raise HTTPException(status_code=503, detail="TARGET_GRAPHQL_URL is not configured correctly")

    body = await request.body()
    headers = forwarded_headers(httpx.Headers(request.headers.raw))
    try:
        # A separate cookie jar per request prevents cross-client session leakage.
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False, trust_env=False) as client:
            client.headers.clear()
            async with client.stream("POST", target, content=body, headers=headers) as upstream:
                # Raw bytes retain Content-Encoding, even for encodings httpx cannot decode.
                content = b"".join([chunk async for chunk in upstream.aiter_raw()])
                response = Response(content=content, status_code=upstream.status_code)
                response.raw_headers.extend(forwarded_headers(upstream.headers))
                return response
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="GraphQL upstream timed out") from None
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="GraphQL upstream request failed") from None
