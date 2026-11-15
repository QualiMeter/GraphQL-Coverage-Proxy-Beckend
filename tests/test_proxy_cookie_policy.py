"""Explicit E2E credentials are distinct from ambient browser cookies."""

import httpx
import pytest

from app.proxy import forwarded_headers
from test_proxy_e2e import capture_context  # noqa: F401
from test_graphql_proxy import client, raw_response, upstream  # noqa: F401


def test_header_filter_denies_all_cookie_variants_by_default():
    headers = httpx.Headers([
        ("Cookie", "portal_session=secret"), ("cOoKiE", "unrelated=secret"),
        ("Cookie2", "obsolete=secret"), ("Set-Cookie", "service_session=secret; Path=/"),
        ("Set-Cookie2", "obsolete=secret"), ("Content-Type", "application/json"),
    ])
    result = httpx.Headers(forwarded_headers(headers))
    assert dict(result) == {"content-type": "application/json"}


@pytest.mark.parametrize("browser_headers", [
    {"Origin": "https://graphql.rebyte.fun"},
    {"Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Mode": "cors"},
    {"Sec-Fetch-Dest": "empty"},
])
async def test_browser_context_cannot_enable_cookies_even_with_valid_api_key(browser_headers, client, upstream, capture_context):
    requests = upstream(lambda request: raw_response(headers={"Set-Cookie": "another_app=overwrite; Path=/"}))
    response = await client.post("/graphql", content=b"{}", headers={
        **browser_headers, "X-Coverage-Run-Id": str(capture_context.run.id), "Cookie": "portal_session=secret; arbitrary_cookie=secret",
    })
    assert response.status_code == 200
    assert "cookie" not in requests[0].headers
    assert "set-cookie" not in response.headers


async def test_trusted_e2e_retains_explicit_upstream_cookies_but_never_internal_credentials(client, upstream):
    requests = upstream(lambda request: raw_response(headers=[
        ("Set-Cookie", "upstream_session=rotate; Path=/"),
        ("Set-Cookie", "__Host-coverage_session=overwrite; Secure; Path=/"),
    ]))
    response = await client.post("/graphql", content=b"{}", headers={
        "Cookie": "upstream_session=e2e-explicit; __Host-coverage_session=internal; coverage_dev_session=internal",
        "Authorization": "Bearer e2e-explicit",
    })
    assert response.status_code == 200
    assert requests[0].headers["cookie"] == "upstream_session=e2e-explicit"
    assert requests[0].headers["authorization"] == "Bearer e2e-explicit"
    assert response.headers.get_list("set-cookie") == ["upstream_session=rotate; Path=/"]
