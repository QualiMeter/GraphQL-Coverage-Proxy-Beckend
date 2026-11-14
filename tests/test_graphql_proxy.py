import gzip
import logging

import httpx
import pytest

from app.main import app


TARGET = "https://upstream.example/api/graphql?tenant=demo"
TEST_ADMIN_KEY = "test-only-coverage-admin-key-" + "a" * 40


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setenv("TARGET_GRAPHQL_URL", TARGET)
    requests = []

    def install(handler):
        transport = httpx.MockTransport(handler)

        async def handle_request(self, request):
            requests.append(request)
            return await transport.handle_async_request(request)

        # Replace only network I/O; exercise the production AsyncClient and route.
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_request)
        return requests

    return install


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_ADMIN_KEY)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.example",
        headers={"X-Coverage-Api-Key": TEST_ADMIN_KEY},
    ) as client:
        yield client


def raw_response(status=200, body=b'{"data":{"ok":true}}', headers=()):
    return httpx.Response(status, stream=httpx.ByteStream(body), headers=headers)


@pytest.mark.parametrize("operation", [
    'query GetUser($id: ID!) { user(id: $id) { id } }',
    'mutation GetUser($id: ID!) { updateUser(id: $id) { id } }',
])
async def test_forwards_original_body_and_applicable_headers(operation, client, upstream, caplog):
    caplog.set_level(logging.DEBUG)
    body = (
        '{ "query": "' + operation + '",\n'
        '"variables": {"id": "private-variable"}, "operationName": "GetUser" }\n'
    ).encode()
    requests = upstream(lambda request: raw_response())
    response = await client.post("/graphql", content=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/graphql-response+json",
        "Authorization": "Bearer private-token",
        "Cookie": "session=private-cookie",
        "Connection": "keep-alive, X-Private-Hop",
        "X-Private-Hop": "drop-me",
        "Proxy-Authorization": "Basic private-proxy-token",
        "Keep-Alive": "timeout=5",
        "TE": "trailers",
        "Trailer": "X-Trailer",
        "Transfer-Encoding": "chunked",
        "Upgrade": "websocket",
        "Proxy-Connection": "keep-alive",
        "Content-Length": "1",
    })
    assert response.status_code == 200
    assert response.content == b'{"data":{"ok":true}}'
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == TARGET
    assert request.content == body
    assert request.headers["host"] == "upstream.example"
    assert request.headers["content-length"] == str(len(body))
    assert request.headers["authorization"] == "Bearer private-token"
    assert request.headers["cookie"] == "session=private-cookie"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/graphql-response+json"
    for name in (
        "connection", "x-private-hop", "proxy-authorization", "keep-alive", "te",
        "trailer", "transfer-encoding", "upgrade", "proxy-connection", "x-coverage-run-id",
        "x-coverage-api-key",
    ):
        assert name not in request.headers
    for secret in (operation, "private-variable", "private-token", "private-cookie", "private-proxy-token"):
        assert secret not in caplog.text


@pytest.mark.parametrize("status,body", [
    (200, b'{ "data":null, "errors":[{"message":"GraphQL error"}] }\n'),
    (201, b'{"data":{"created":true}}'),
    (400, b'{"errors":[{"message":"Bad query"}]}'),
    (401, b"Unauthorized"),
    (403, b"Forbidden"),
    (422, b"Invalid operation"),
    (429, b"Rate limited"),
    (500, b"Internal error"),
    (503, b"Unavailable"),
    (204, b""),
])
async def test_preserves_upstream_status_body_and_headers(status, body, client, upstream):
    upstream(lambda request: raw_response(status, body, [
        ("Content-Type", "application/graphql-response+json"),
        ("Content-Length", str(len(body))),
        ("Connection", "keep-alive, X-Upstream-Hop"),
        ("Connection", "X-Another-Hop"),
        ("X-Upstream-Hop", "drop"), ("X-Another-Hop", "drop"),
        ("Transfer-Encoding", "chunked"), ("Keep-Alive", "timeout=5"),
        ("Proxy-Authenticate", "Basic"), ("Trailer", "X-Trailer"),
        ("X-Coverage-Run-Id", "internal"),
        ("Set-Cookie", "first=1; HttpOnly"), ("Set-Cookie", "second=2; Secure"),
        ("WWW-Authenticate", "Bearer"), ("Retry-After", "10"),
    ]))
    response = await client.post("/graphql", content=b"{}")
    assert response.status_code == status
    assert response.content == body
    assert response.headers["content-type"] == "application/graphql-response+json"
    if status != 204:
        assert response.headers.get_list("content-length") == [str(len(body))]
    else:
        assert "content-length" not in response.headers
    assert response.headers.get_list("set-cookie") == ["first=1; HttpOnly", "second=2; Secure"]
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["retry-after"] == "10"
    for name in (
        "connection", "x-upstream-hop", "x-another-hop", "transfer-encoding",
        "keep-alive", "proxy-authenticate", "trailer", "x-coverage-run-id",
    ):
        assert name not in response.headers


async def test_preserves_compressed_bytes_encoding_and_correct_length(client, upstream):
    body = b'{"data":{"message":"compressed response"}}'
    compressed = gzip.compress(body)
    upstream(lambda request: raw_response(200, compressed, {
        "Content-Encoding": "gzip", "Content-Length": str(len(compressed)),
    }))
    async with client.stream("POST", "/graphql", content=b"{}") as response:
        raw = b"".join([chunk async for chunk in response.aiter_raw()])
    assert raw == compressed
    assert gzip.decompress(raw) == body
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers.get_list("content-length") == [str(len(compressed))]


async def test_does_not_follow_redirects(client, upstream):
    requests = upstream(lambda request: raw_response(307, b"redirect", {
        "Location": "https://other.example/graphql",
    }))
    response = await client.post("/graphql", content=b"{}", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 307
    assert response.content == b"redirect"
    assert response.headers["location"] == "https://other.example/graphql"
    assert len(requests) == 1


@pytest.mark.parametrize("error,status,detail", [
    (httpx.ConnectTimeout, 504, "GraphQL upstream timed out"),
    (httpx.ReadTimeout, 504, "GraphQL upstream timed out"),
    (httpx.ConnectError, 502, "GraphQL upstream request failed"),
    (httpx.RemoteProtocolError, 502, "GraphQL upstream request failed"),
])
async def test_gateway_errors_do_not_expose_details(error, status, detail, client, upstream, caplog):
    def fail(request):
        raise error("secret-token at private-host:5432", request=request)

    upstream(fail)
    response = await client.post("/graphql", content=b"{}")
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "secret-token" not in caplog.text
    assert "private-host" not in caplog.text


async def test_timeout_while_reading_response_returns_504(client, upstream):
    class InterruptedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadTimeout("secret upstream details")

    upstream(lambda request: httpx.Response(200, stream=InterruptedBody()))
    response = await client.post("/graphql", content=b"{}")
    assert response.status_code == 504
    assert response.json() == {"detail": "GraphQL upstream timed out"}


@pytest.mark.parametrize("target", [None, "", "  ", "not-a-url", "ftp://upstream.example", "https://upstream.example:invalid"])
async def test_missing_or_invalid_config_only_disables_proxy(target, client, upstream, monkeypatch):
    requests = upstream(lambda request: raw_response())
    if target is None:
        monkeypatch.delenv("TARGET_GRAPHQL_URL")
    else:
        monkeypatch.setenv("TARGET_GRAPHQL_URL", target)
    response = await client.post("/graphql", content=b"{}")
    assert response.status_code == 503
    assert response.json() == {"detail": "TARGET_GRAPHQL_URL is not configured correctly"}
    assert requests == []
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/openapi.json")).status_code == 200
    assert (await client.get("/api/runs")).status_code == 422


async def test_request_cannot_override_target_or_reuse_upstream_cookies(client, upstream):
    requests = upstream(lambda request: raw_response(headers={"Set-Cookie": "session=upstream-secret"}))
    await client.post("/graphql?TARGET_GRAPHQL_URL=https://other.example", content=b"{}", headers={
        "TARGET_GRAPHQL_URL": "https://other.example/graphql",
        "X-Target-GraphQL-URL": "https://other.example/graphql",
    })
    client.cookies.clear()  # Simulate a second caller with no session cookie.
    await client.post("/graphql", content=b"{}")
    assert len(requests) == 2
    assert all(str(request.url) == TARGET for request in requests)
    assert all("cookie" not in request.headers for request in requests)
    assert all(request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 30.0)
               for request in requests)


@pytest.mark.parametrize("userinfo", [
    pytest.param("test-user:test-password", id="username-and-password"),
    pytest.param("test-user", id="username-only"),
    pytest.param(":test-password", id="password-only"),
    pytest.param("", id="empty-userinfo"),
    pytest.param(":", id="empty-username-and-password"),
    pytest.param("test-user:", id="empty-password"),
    pytest.param("test%2Duser:test%2Dpassword", id="encoded-credentials"),
])
async def test_rejects_target_userinfo_without_request_or_secret_logs(
    userinfo, client, upstream, monkeypatch, caplog,
):
    caplog.set_level(logging.DEBUG)
    requests = upstream(lambda request: raw_response())
    target = f"https://{userinfo}@upstream.example/graphql"
    monkeypatch.setenv("TARGET_GRAPHQL_URL", target)

    response = await client.post("/graphql", content=b"{}", headers={
        "Authorization": "Bearer test-token", "Cookie": "session=test-cookie",
    })

    assert response.status_code == 503
    assert response.json() == {"detail": "TARGET_GRAPHQL_URL is not configured correctly"}
    assert len(requests) == 0
    # Boolean aggregation avoids printing captured credentials on assertion failure.
    assert all(value not in caplog.text for value in (
        target, "test-user", "test-password", "test%2Duser", "test%2Dpassword",
        "test-token", "test-cookie",
    ))


@pytest.mark.parametrize("casing", ["lower", "upper", "mixed"])
async def test_strips_client_forwarding_headers_and_preserves_request(casing, client, upstream):
    spoofed = {
        "X-Forwarded-For": "203.0.113.99",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "trusted.example",
        "X-Forwarded-Port": "443",
        "X-Forwarded-Prefix": "/admin",
        "X-Forwarded-Arbitrary-Suffix": "spoofed",
        "X-Forwarded-": "spoofed",
        "X-Real-IP": "203.0.113.99",
        "Forwarded": 'for=203.0.113.99;proto=https;host="trusted.example"',
    }
    transform = {"lower": str.lower, "upper": str.upper, "mixed": str.swapcase}[casing]
    headers = {transform(name): value for name, value in spoofed.items()}
    headers.update({
        "Authorization": "Bearer test-token",
        "Cookie": "session=test-cookie",
        "Content-Type": "application/json",
        "Accept": "application/graphql-response+json",
        "Connection": "X-Private-Hop",
        "X-Private-Hop": "drop-me",
        "X-Request-Id": "preserved",
    })
    body = b'{ "query": "query Test { health }", "variables": {}, "operationName": "Test" }\n'
    requests = upstream(lambda request: raw_response(headers={"X-Forwarded-Upstream": "preserved"}))

    response = await client.post("/graphql", content=body, headers=headers)

    assert response.status_code == 200
    assert response.content == b'{"data":{"ok":true}}'
    assert response.headers["x-forwarded-upstream"] == "preserved"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == TARGET
    assert request.content == body
    assert not any(
        name.startswith("x-forwarded-") or name in {"forwarded", "x-real-ip"}
        for name in request.headers
    )
    for name in ("connection", "x-private-hop", "x-coverage-run-id"):
        assert name not in request.headers
    assert request.headers["authorization"] == "Bearer test-token"
    assert request.headers["cookie"] == "session=test-cookie"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept"] == "application/graphql-response+json"
    assert request.headers["x-request-id"] == "preserved"
