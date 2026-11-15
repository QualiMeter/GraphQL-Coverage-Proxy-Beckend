import asyncio
import json

import httpcore
import httpx
import pytest

from app import upstream
from app.schema_collector import (
    collect_schema, InvalidSchemaTargetURLError, SchemaDNSUnavailableError, SchemaTimeoutError,
    SchemaConnectionError, SchemaRedirectError, SchemaResponseTooLargeError, SchemaCapacityError,
    IntrospectionDisabledError, GraphQLIntrospectionError, InvalidIntrospectionResponseError,
)
from app.upstream import PinnedTransport, outbound_slot
from test_schema_collector import introspection_payload
from test_upstream_security import SocketStream


@pytest.mark.parametrize("url", ["https://127.0.0.1/graphql", "https://10.0.0.1/graphql", "https://169.254.169.254/metadata",
    "https://[::1]/graphql", "https://[::ffff:192.168.1.1]/graphql", "https://[fd00:ec2::254]/metadata",
    "https://93.184.216.34:5432/graphql", "https://user:private-password@93.184.216.34/graphql"])
async def test_forbidden_endpoint_never_connects(monkeypatch, url):
    async def forbidden(*args, **kwargs):
        pytest.fail("Forbidden network destination was contacted")
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    with pytest.raises(InvalidSchemaTargetURLError):
        await collect_schema(url)


async def test_actual_transport_is_pinned_between_dns_and_tls_connect(monkeypatch):
    calls = []
    async def dns(host, port):
        calls.append(host)
        return [(2, 1, 6, "", ("93.184.216.34" if len(calls) == 1 else "127.0.0.1", port))]
    monkeypatch.setattr(upstream, "_resolve", dns)
    stream = SocketStream()
    body = json.dumps(introspection_payload()).encode()
    stream.response = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
    connections = []
    async def connect(self, host, port, *args, **kwargs):
        connections.append((host, port))
        return stream
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    result = await collect_schema("https://rebinding.example/graphql")
    assert result.parsed.sha256
    assert calls == ["rebinding.example"] and connections == [("93.184.216.34", 443)]
    assert stream.tls[0][1] == "rebinding.example" and stream.tls[0][0].check_hostname
    assert stream.closed


async def test_private_dns_answer_never_connects(monkeypatch):
    async def dns(*args):
        return [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("127.0.0.1", 443))]
    async def forbidden(*args, **kwargs):
        pytest.fail("Mixed DNS answer connected")
    monkeypatch.setattr(upstream, "_resolve", dns)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", forbidden)
    with pytest.raises(InvalidSchemaTargetURLError):
        await collect_schema("https://mixed.example/graphql")


@pytest.mark.parametrize("error,expected", [(OSError("private-host"), SchemaDNSUnavailableError),
                                           (TimeoutError("private-host"), SchemaTimeoutError)])
async def test_dns_errors_are_safe(monkeypatch, error, expected):
    async def dns(*args):
        raise error
    monkeypatch.setattr(upstream, "_resolve", dns)
    with pytest.raises(expected) as failure:
        await collect_schema("https://dns.example/graphql")
    assert "private-host" not in str(failure.value)


@pytest.mark.parametrize("error,expected", [(httpcore.ConnectError("private-host"), SchemaConnectionError),
    (httpcore.ConnectTimeout("private-host"), SchemaTimeoutError)])
async def test_actual_socket_errors_are_safe_and_not_retried(monkeypatch, error, expected):
    calls = []
    async def connect(*args, **kwargs):
        calls.append(args)
        raise error
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    with pytest.raises(expected) as failure:
        await collect_schema("https://93.184.216.34/graphql")
    assert len(calls) == 1 and "private-host" not in str(failure.value)


async def test_redirect_to_metadata_is_not_followed():
    calls = []
    def network(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "http://169.254.169.254/latest/meta-data"})
    with pytest.raises(SchemaRedirectError):
        await collect_schema("https://93.184.216.34/graphql", transport=httpx.MockTransport(network))
    assert len(calls) == 1


async def test_too_large_stream_is_closed_and_not_retried(monkeypatch):
    import app.schema_collector as collector
    monkeypatch.setattr(collector, "MAX_RESPONSE_BYTES", 100)
    class Body(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield b"x" * 50
            yield b"x" * 51
            pytest.fail("Oversized stream continued to read")
        async def aclose(self):
            self.closed = True
    body = Body()
    calls = []
    def network(request):
        calls.append(request)
        return httpx.Response(200, stream=body)
    with pytest.raises(SchemaResponseTooLargeError):
        await collect_schema("https://93.184.216.34/graphql", transport=httpx.MockTransport(network))
    assert body.closed and len(calls) == 1


@pytest.mark.parametrize("status", [200, 400, 403])
@pytest.mark.parametrize("error", [
    {"message": "GraphQL introspection has been disabled, but the requested query contained __schema"},
    {"message": "GraphQL introspection is not allowed by Apollo Server"},
    {"message": "Private text", "extensions": {"code": "INTROSPECTION_DISABLED"}},
])
async def test_explicit_introspection_refusal_has_own_safe_code(status, error):
    with pytest.raises(IntrospectionDisabledError) as failure:
        await collect_schema("https://93.184.216.34/graphql", transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"errors": [error]})))
    assert failure.value.status_code == 409 and failure.value.code == "INTROSPECTION_DISABLED"
    assert "SDL" in failure.value.public_detail and "Private text" not in str(failure.value)


@pytest.mark.parametrize("errors", [[{"message": "private-secret"}], [{"message": None}], "private-secret", [None]])
async def test_generic_errors_do_not_masquerade_as_disabled(errors, caplog):
    with pytest.raises(GraphQLIntrospectionError) as failure:
        await collect_schema("https://93.184.216.34/graphql", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"errors": errors})))
    assert failure.value.code == "UPSTREAM_GRAPHQL_ERRORS" and failure.value.status_code == 502
    assert "private-secret" not in str(failure.value) + caplog.text


async def test_response_extensions_are_never_persisted():
    data = introspection_payload()
    data["extensions"] = {"trace": "private-token"}
    data["data"]["unrequestedField"] = "private-secret"
    result = await collect_schema("https://93.184.216.34/graphql", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=data)))
    assert set(result.introspection_json) == {"data"}
    assert set(result.introspection_json["data"]) == {"__schema"}
    assert "private-token" not in json.dumps(result.introspection_json)


async def test_shared_capacity_rejects_introspection_before_network():
    ready = asyncio.Event()
    release = asyncio.Event()
    count = 0
    async def hold():
        nonlocal count
        async with outbound_slot():
            count += 1
            if count == 16:
                ready.set()
            await release.wait()
    tasks = [asyncio.create_task(hold()) for _ in range(16)]
    calls = []
    try:
        await asyncio.wait_for(ready.wait(), 2)
        with pytest.raises(SchemaCapacityError):
            await collect_schema("https://93.184.216.34/graphql", transport=httpx.MockTransport(
                lambda request: calls.append(request)))
        assert calls == []
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_slow_response_obeys_total_deadline_and_closes_stream():
    class Body(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"unreachable"
        async def aclose(self):
            self.closed = True
    stream = Body()
    with pytest.raises(SchemaTimeoutError):
        await collect_schema("https://93.184.216.34/graphql", timeout=.02,
                             transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)))
    assert stream.closed


@pytest.mark.parametrize("schema_data", [{"types": [None]}, {"types": [1]}, {"types": [{"name": "Query", "kind": []}]},
    {"types": [], "queryType": 1}, {"types": [], "directives": [None]}])
async def test_malformed_nested_schema_is_a_safe_error(schema_data):
    with pytest.raises(InvalidIntrospectionResponseError):
        await collect_schema("https://93.184.216.34/graphql", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": {"__schema": schema_data}})))


async def test_negated_disabled_message_is_not_proof_of_refusal():
    with pytest.raises(GraphQLIntrospectionError) as failure:
        await collect_schema("https://93.184.216.34/graphql", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"errors": [{"message": "Introspection is not disabled; resolver failed"}]})))
    assert failure.value.code == "UPSTREAM_GRAPHQL_ERRORS"


@pytest.mark.parametrize("schema_data", [{"types": [None] * 200_001}, None])
async def test_complexity_budget_rejects_before_recursive_schema_build(schema_data):
    if schema_data is None:
        schema_data = {}
        for _ in range(66):
            schema_data = {"nested": schema_data}
    with pytest.raises(SchemaResponseTooLargeError):
        await collect_schema("https://93.184.216.34/graphql", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": {"__schema": schema_data}})))
