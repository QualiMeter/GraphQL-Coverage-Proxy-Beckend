import asyncio
import socket
import ssl

import httpcore
import httpx
import pytest

from app.upstream import PinnedBackend, PinnedTransport, UnsafeEndpoint, validate_endpoint, outbound_slot
from test_graphql_proxy import client, raw_response, upstream  # noqa: F401


@pytest.mark.parametrize("url", [
    "http://example.com/graphql", "https://example.com:22/gql", "https://example.com:0/gql", "https://168.63.129.16/gql", "https://example.com:8443/gql",
    "https://user:password@example.com/gql", "https://@example.com/gql",
    "https://example.com/gql?token=secret", "https://example.com/gql?", "https://example.com/gql#secret",
    "https://127.0.0.1/gql", "https://127.1/gql", "https://2130706433/gql", "https://0x7f000001/gql",
    "https://10.0.0.1/gql", "https://172.16.0.1/gql", "https://192.168.1.1/gql",
    "https://169.254.169.254/latest/meta-data", "https://[::1]/gql", "https://[fe80::1]/gql",
    "https://[fd00::1]/gql", "https://[fd00:ec2::254]/metadata", "https://[::ffff:127.0.0.1]/gql", "https://[64:ff9b::a00:1]/gql",
    "https://[2002:7f00:1::]/gql", "https://[::]/gql", "https://[ff02::1]/gql",
    "https://example.com\\@127.0.0.1/gql", " https://example.com/gql", "https://example.com/gql\n",
    "https://[fe80::1%25eth0]/gql", "https://example.com./gql",
])
async def test_unsafe_url_is_rejected(url):
    with pytest.raises(UnsafeEndpoint) as error:
        await validate_endpoint(url)
    assert "password" not in str(error.value) and "secret" not in str(error.value)


async def test_all_dns_answers_must_be_safe(monkeypatch):
    async def dns(*args, **kwargs):
        return [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET6, 1, 6, "", ("::ffff:10.0.0.1", 443, 0, 0))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    with pytest.raises(UnsafeEndpoint):
        await validate_endpoint("https://mixed.example/graphql")


async def test_dns_changes_between_save_and_request_are_rechecked(monkeypatch):
    calls = 0
    async def dns(*args, **kwargs):
        nonlocal calls
        calls += 1
        return [(socket.AF_INET, 1, 6, "", ("93.184.216.34" if calls == 1 else "127.0.0.1", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    await validate_endpoint("https://changing.example/graphql")
    with pytest.raises(UnsafeEndpoint):
        await validate_endpoint("https://changing.example/graphql")
    assert calls == 2


@pytest.mark.parametrize("url", ["http://127.0.0.1:8123/graphql", "http://[::1]:8123/graphql", "http://10.0.0.1:8123/graphql"])
async def test_local_http_requires_both_dev_and_exact_origin(monkeypatch, url):
    monkeypatch.setenv("COVERAGE_DEV_MODE", "true")
    with pytest.raises(UnsafeEndpoint):
        await validate_endpoint(url)
    origin = str(httpx.URL(url).copy_with(path=""))
    monkeypatch.setenv("COVERAGE_DEV_UPSTREAM_ORIGINS", origin)
    assert (await validate_endpoint(url)).url == httpx.URL(url)
    with pytest.raises(UnsafeEndpoint):
        await validate_endpoint(url.replace("8123", "8124"))
    monkeypatch.setenv("COVERAGE_DEV_MODE", "false")
    with pytest.raises(UnsafeEndpoint):
        await validate_endpoint(url)


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data", "http://[fd00:ec2::254]/latest/meta-data"])
async def test_metadata_still_forbidden_in_dev(monkeypatch, url):
    monkeypatch.setenv("COVERAGE_DEV_MODE", "true")
    monkeypatch.setenv("COVERAGE_DEV_UPSTREAM_ORIGINS", str(httpx.URL(url).copy_with(path="")))
    with pytest.raises(UnsafeEndpoint):
        await validate_endpoint(url)


class SocketStream(httpcore.AsyncNetworkStream):
    def __init__(self, peer="93.184.216.34"):
        self.peer = peer
        self.tls = []
        self.writes = []
        self.closed = False
        self.response = b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"data":{}}'

    async def read(self, max_bytes, timeout=None):
        data, self.response = self.response, b""
        return data

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.tls.append((ssl_context, server_hostname))
        return self

    def get_extra_info(self, info):
        return (self.peer, 443) if info == "server_addr" else None


async def test_rebinding_cannot_change_connected_ip_and_tls_keeps_hostname(monkeypatch):
    calls = []
    async def dns(*args, **kwargs):
        calls.append(args)
        return [(socket.AF_INET, 1, 6, "", ("93.184.216.34" if len(calls) == 1 else "127.0.0.1", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    destination = await validate_endpoint("https://service.example/graphql")
    stream = SocketStream()
    connections = []
    async def connect(self, host, port, *args, **kwargs):
        connections.append((host, port))
        return stream
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with httpx.AsyncClient(transport=PinnedTransport(destination), trust_env=False) as client:
        response = await client.post(str(destination.url), content=b'{"query":"{x}"}',
                                     extensions={"sni_hostname": "attacker.example"})
    assert response.json() == {"data": {}}
    assert connections == [("93.184.216.34", 443)] and len(calls) == 1
    assert stream.tls[0][1] == "service.example"
    assert stream.tls[0][0].check_hostname and stream.tls[0][0].verify_mode == ssl.CERT_REQUIRED
    assert b"Host: service.example\r\n" in b"".join(stream.writes)
    assert stream.closed


async def test_unexpected_actual_peer_is_closed_without_sending_http(monkeypatch):
    destination = await validate_endpoint("https://93.184.216.34/graphql")
    stream = SocketStream("127.0.0.1")
    async def connect(*args, **kwargs):
        return stream
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with httpx.AsyncClient(transport=PinnedTransport(destination)) as client:
        with pytest.raises(httpx.RequestError):
            await client.post(str(destination.url), content=b"mutation")
    assert stream.closed and stream.writes == [] and stream.tls == []


async def test_no_connection_retry(monkeypatch):
    destination = await validate_endpoint("https://93.184.216.34/graphql")
    calls = []
    async def connect(*args, **kwargs):
        calls.append(args)
        raise httpcore.ConnectError("private details")
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with httpx.AsyncClient(transport=PinnedTransport(destination)) as client:
        with pytest.raises(httpx.RequestError, match="Upstream connection failed"):
            await client.post(str(destination.url))
    assert len(calls) == 1


@pytest.mark.parametrize("location", ["http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data"])
async def test_redirect_never_contacts_forbidden_location(client, upstream, location):
    calls = upstream(lambda request: raw_response(307, headers={"Location": location}))
    response = await client.post("/graphql", json={"query": "mutation { doThing }"})
    assert response.status_code == 307 and len(calls) == 1


async def test_request_limit_prevents_upstream(client, upstream):
    from app.upstream import MAX_REQUEST_BYTES
    calls = upstream(lambda request: raw_response())
    response = await client.post("/graphql", content=b"x" * (MAX_REQUEST_BYTES + 1))
    assert response.status_code == 413 and calls == []


async def test_response_limit_does_not_retry_mutation(client, upstream):
    from app.upstream import MAX_RESPONSE_BYTES
    calls = upstream(lambda request: raw_response(body=b"x" * (MAX_RESPONSE_BYTES + 1)))
    response = await client.post("/graphql", json={"query": "mutation { doThing }"})
    assert response.status_code == 502 and len(calls) == 1
    assert "Do not retry automatically" in response.json()["detail"]


async def test_outbound_capacity_is_bounded():
    from fastapi import HTTPException
    entered = asyncio.Event()
    release = asyncio.Event()
    count = 0
    async def hold():
        nonlocal count
        async with outbound_slot():
            count += 1
            if count == 16:
                entered.set()
            await release.wait()
    tasks = [asyncio.create_task(hold()) for _ in range(16)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(HTTPException) as error:
            async with outbound_slot():
                pytest.fail("Capacity exceeded")
        assert error.value.status_code == 503
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.parametrize("port", [22, 25, 53, 2375, 3306, 5432, 6379])
async def test_dev_allowlist_cannot_enable_unsafe_ports(port, monkeypatch):
    monkeypatch.setenv("COVERAGE_DEV_MODE", "true")
    monkeypatch.setenv("COVERAGE_DEV_UPSTREAM_ORIGINS", f"http://127.0.0.1:{port}")
    with pytest.raises(UnsafeEndpoint):
        await validate_endpoint(f"http://127.0.0.1:{port}/graphql")
