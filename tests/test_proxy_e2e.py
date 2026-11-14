"""Proxy opt-in contract; analyzers and persistence are test doubles only."""

import gzip
import uuid
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.datastructures import State
from sqlalchemy.ext.asyncio import AsyncSession

from app import proxy
from app.contracts import AnalysisResult
from app.main import app
from app.models import SchemaSnapshot, TestRun as Run
from test_graphql_proxy import client, raw_response, upstream  # noqa: F401


@pytest.fixture
def capture_context(monkeypatch):
    run_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    session = AsyncMock(spec=AsyncSession)
    run = SimpleNamespace(id=run_id, snapshot_id=snapshot_id, status="running")
    snapshot = SimpleNamespace(id=snapshot_id, schema_sdl="type Query { ok: Boolean }")

    async def get(model, key, **kwargs):
        if model is Run:
            return run if key == run_id else None
        assert model is SchemaSnapshot
        assert key == snapshot_id
        return snapshot

    session.get.side_effect = get

    async def dependency():
        yield session

    analyzer = AsyncMock(return_value=AnalysisResult(status="success"))
    capture = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyzer}))
    monkeypatch.setattr(proxy, "capture_e2e_exchange", capture)
    app.dependency_overrides[proxy.get_proxy_session] = dependency
    try:
        yield SimpleNamespace(run=run, snapshot=snapshot, session=session, capture=capture, analyzer=analyzer)
    finally:
        app.dependency_overrides.pop(proxy.get_proxy_session, None)


async def test_plain_proxy_never_opens_coverage_session(client, upstream, monkeypatch):
    def forbidden():
        pytest.fail("Plain proxy traffic must not open a database session")

    monkeypatch.setattr(proxy, "SessionFactory", forbidden)
    monkeypatch.setattr(app, "state", State({"coverage_analyzer": None}))
    monkeypatch.setattr(proxy, "capture_e2e_exchange", forbidden)
    upstream(lambda request: raw_response(202, b"original"))
    response = await client.post("/graphql", content=b"not even JSON")
    assert (response.status_code, response.content) == (202, b"original")


@pytest.mark.parametrize("values", [
    [""], ["drop-me"], ["not-a-uuid"], ["private-token"],
    [str(uuid.uuid4()) + "," + str(uuid.uuid4())],
    [str(uuid.uuid4()), str(uuid.uuid4())],
    [str(uuid.UUID(int=1))] * 2,
    ["urn:uuid:" + str(uuid.uuid4())], ["{" + str(uuid.uuid4()) + "}"],
])
async def test_rejects_malformed_or_duplicate_run_id(values, client, upstream, capture_context, caplog):
    requests = upstream(lambda request: raw_response())
    headers = [("X-Coverage-Run-Id", value) for value in values]
    response = await client.post("/graphql", content=b"private query", headers=headers)
    assert response.status_code == 400
    assert requests == []
    capture_context.session.get.assert_not_awaited()
    capture_context.capture.assert_not_awaited()
    assert "private-token" not in response.text + caplog.text


@pytest.mark.parametrize("status", ["missing", "completed", "failed", "missing_snapshot"])
async def test_rejects_unavailable_run_before_upstream(status, client, upstream, capture_context):
    context = capture_context
    if status == "missing_snapshot":
        context.session.get.side_effect = [context.run, None]
    elif status != "missing":
        context.run.status = status
    requests = upstream(lambda request: raw_response())
    response = await client.post("/graphql", content=b"{}", headers={
        "X-Coverage-Run-Id": str(uuid.uuid4() if status == "missing" else context.run.id),
    })
    assert response.status_code == (404 if status in {"missing", "missing_snapshot"} else 409)
    assert requests == []
    context.capture.assert_not_awaited()


@pytest.mark.parametrize("analyzer", [None, "not callable"])
async def test_missing_analyzer_rejects_before_upstream_or_database(
    analyzer, client, upstream, capture_context, monkeypatch,
):
    monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyzer}))
    requests = upstream(lambda request: raw_response())
    response = await client.post("/graphql", content=b"{}", headers={
        "X-Coverage-Run-Id": str(capture_context.run.id),
    })
    assert response.status_code == 503
    assert response.json() == {"detail": "Coverage analyzer is unavailable"}
    assert requests == []
    capture_context.session.get.assert_not_awaited()
    capture_context.capture.assert_not_awaited()


@pytest.mark.parametrize("encoding", [None, "gzip", "deflate", "unknown"])
@pytest.mark.parametrize("status", [201, 401, 500])
async def test_capture_awaited_with_truthful_metadata_and_raw_response(
    encoding, status, client, upstream, capture_context, monkeypatch,
):
    body = b'{ "query": "query Test { ok }", "variables": {"secret": "private-value"} }'
    decoded = b'{"data":null,"errors":[{"message":"private response"}]}'
    wire = gzip.compress(decoded) if encoding == "gzip" else (
        zlib.compress(decoded) if encoding == "deflate" else decoded
    )
    headers = [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"), ("X-Upstream", "preserved")]
    if encoding:
        headers.append(("Content-Encoding", encoding))
    requests = upstream(lambda request: raw_response(status, wire, headers))
    ticks = iter([100.0, 100.125])
    monkeypatch.setattr(proxy, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
    saved = []

    async def save(*args, **kwargs):
        assert len(requests) == 1
        saved.append(True)

    capture_context.capture.side_effect = save
    async with client.stream("POST", "/graphql", content=body, headers={
        "x-COVERAGE-run-ID": str(capture_context.run.id).upper(),
        "Authorization": "Bearer private-token", "Cookie": "session=private-cookie",
        "X-Forwarded-For": "spoofed", "X-Forwarded-Host": "spoofed",
        "Forwarded": "for=spoofed", "X-Request-Id": "preserved",
    }) as response:
        assert saved == [True]
        raw = b"".join([chunk async for chunk in response.aiter_raw()])
    assert raw == wire
    assert response.status_code == status
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert response.headers["x-upstream"] == "preserved"
    if encoding:
        assert response.headers["content-encoding"] == encoding
    assert requests[0].content == body
    assert requests[0].headers["authorization"] == "Bearer private-token"
    assert requests[0].headers["cookie"] == "session=private-cookie"
    assert requests[0].headers["x-request-id"] == "preserved"
    assert not any(name.startswith("x-forwarded-") or name in {"forwarded", "x-coverage-run-id"}
                   for name in requests[0].headers)
    capture_context.capture.assert_awaited_once_with(
        capture_context.session, run_id=capture_context.run.id, request_body=body,
        decoded_response_body=None if encoding == "unknown" else decoded,
        http_status=status, duration_ms=125, analyzer=capture_context.analyzer,
    )


async def test_storage_unavailable_preflight_is_safe_503(client, upstream, capture_context, caplog):
    capture_context.session.get.side_effect = RuntimeError("private-token query variables cookie")
    requests = upstream(lambda request: raw_response())
    response = await client.post("/graphql", content=b"{}", headers={
        "X-Coverage-Run-Id": str(capture_context.run.id),
    })
    assert response.status_code == 503
    assert requests == []
    assert "private-token" not in response.text + caplog.text


async def test_save_failure_is_explicit_without_retry_or_secret_leak(client, upstream, capture_context, caplog):
    capture_context.capture.side_effect = RuntimeError("private-token query variables cookie")
    requests = upstream(lambda request: raw_response(200, b"upstream already executed"))
    response = await client.post("/graphql", content=b"private query", headers={
        "X-Coverage-Run-Id": str(capture_context.run.id),
    })
    assert response.status_code == 502
    assert "upstream may have executed" in response.json()["detail"]
    assert "Do not retry automatically" in response.json()["detail"]
    assert len(requests) == 1
    assert "private-token" not in response.text + caplog.text
    assert "private query" not in response.text + caplog.text


@pytest.mark.parametrize("error,status", [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
async def test_instrumented_network_failure_preserves_contract(error, status, client, upstream, capture_context):
    def fail(request):
        raise error("private upstream details", request=request)

    requests = upstream(fail)
    response = await client.post("/graphql", content=b"{}", headers={
        "X-Coverage-Run-Id": str(capture_context.run.id),
    })
    assert response.status_code == status
    assert len(requests) == 1
    capture_context.capture.assert_not_awaited()
    assert "private upstream details" not in response.text


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_decoding_is_bounded_and_requires_complete_single_stream(encoding, monkeypatch):
    monkeypatch.setattr(proxy, "MAX_ANALYSIS_RESPONSE_BYTES", 128)
    compress = gzip.compress if encoding == "gzip" else zlib.compress
    headers = httpx.Headers({"Content-Encoding": encoding})
    assert proxy.decoded_response_copy(compress(b"a" * 128), headers) == b"a" * 128
    assert proxy.decoded_response_copy(compress(b"a" * 129), headers) is None
    assert proxy.decoded_response_copy(compress(b"a" * 100_000), headers) is None
    assert proxy.decoded_response_copy(compress(b"data")[:-1], headers) is None
    assert proxy.decoded_response_copy(compress(b"data") + compress(b"more"), headers) is None
    assert proxy.decoded_response_copy(b"invalid", headers) is None
    assert proxy.decoded_response_copy(compress(b"data") + b"junk", headers) is None


def test_decoding_unknown_stacked_and_oversized_bodies(monkeypatch):
    monkeypatch.setattr(proxy, "MAX_ANALYSIS_RESPONSE_BYTES", 16)
    assert proxy.decoded_response_copy(b"a" * 17, httpx.Headers()) is None
    assert proxy.decoded_response_copy(b"body", httpx.Headers()) == b"body"
    assert proxy.decoded_response_copy(b"body", httpx.Headers({"Content-Encoding": "br"})) is None
    assert proxy.decoded_response_copy(b"body", httpx.Headers({"Content-Encoding": "gzip, deflate"})) is None
