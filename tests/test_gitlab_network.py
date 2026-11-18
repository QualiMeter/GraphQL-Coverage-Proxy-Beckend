"""Exercise real HTTPX/httpcore with fake DNS and sockets; no external requests."""
import asyncio
import json
import socket
import ssl
from types import SimpleNamespace
from uuid import uuid4

import httpcore
import httpx
import pytest

from app import gitlab_client, upstream
from app.diagnostics import diagnostic_run, outbound_event
from app.gitlab_client import GitLabClient, GitLabError
from app.upstream import Destination, PinnedBackend, PinnedTransport
from test_gitlab_client import TOKEN, config, data, gitlab_http  # noqa: F401
from test_upstream_security import SocketStream

V4 = "93.184.216.34"
V6 = "2606:4700:4700::1111"


def events(caplog):
    return [r.diagnostic for r in caplog.records
            if hasattr(r, "diagnostic") and r.diagnostic["event"] == "coverage_outbound"]


@pytest.fixture
def sockets(monkeypatch):
    """All DNS answers pass through the production address policy."""
    calls, streams = [], []

    def install(addresses, failures=()):
        async def dns(*args, **kwargs):
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, 1, 6, "", (ip, 443))
                    for ip in addresses]

        async def connect(self, host, port, timeout=None, *args, **kwargs):
            calls.append((host, port, timeout))
            index = len(calls) - 1
            if index < len(failures) and failures[index] is not None:
                raise failures[index]
            stream = SocketStream(host)
            body = json.dumps(data()).encode()
            stream.response = (b"HTTP/1.1 201 Created\r\nContent-Length: " + str(len(body)).encode()
                               + b"\r\nConnection: close\r\n\r\n" + body)
            streams.append(stream)
            return stream

        monkeypatch.setattr(upstream, "_resolve", dns)
        monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
        return calls, streams
    return install


@pytest.mark.parametrize("addresses", [(V6, V4), (V4, V6)])
@pytest.mark.parametrize("error", [httpcore.ConnectError, httpcore.ConnectTimeout])
async def test_failed_first_validated_ip_falls_back_before_single_post(
    config, sockets, addresses, error, caplog,
):
    calls, streams = sockets(addresses, [error(TOKEN)])
    run_id = uuid4()
    with diagnostic_run(run_id):
        result = await GitLabClient(config).launch(123, "main", run_id)
    assert result.id == 42
    assert [c[0] for c in calls] == list(addresses)
    assert 0 < calls[0][2] <= 5 and 0 < calls[1][2] <= 10
    stream, = streams
    wire = b"".join(stream.writes)
    assert wire.count(b"POST /api/v4/projects/123/pipeline HTTP/1.1") == 1
    assert b"Host: gitlab.example\r\n" in wire
    context, hostname = stream.tls[0]
    assert hostname == "gitlab.example"
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert stream.closed
    records = events(caplog)
    assert any(e["outcome"] == "fallback_connected" and e["attempt"] == 2 for e in records)
    assert all(e["run_id"] == str(run_id) for e in records)
    assert TOKEN not in caplog.text


async def test_all_validated_ips_fail_without_any_http_bytes(config, sockets, caplog):
    calls, streams = sockets((V6, V4), [httpcore.ConnectError(TOKEN), httpcore.ConnectTimeout(TOKEN)])
    with pytest.raises(GitLabError) as caught:
        await GitLabClient(config).launch(123, "main", uuid4())
    assert caught.value.code == "gitlab_network_error" and caught.value.ambiguous
    assert len(calls) == 2 and streams == []
    assert any(e["stage"] == "tcp" and e.get("method") == "POST" for e in events(caplog))
    assert TOKEN not in caplog.text + str(caught.value)


async def test_tcp_attempts_share_one_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(upstream, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    backend = PinnedBackend(Destination(httpx.URL("https://gitlab.example"), (V6, V4)))
    calls = []
    stream = SocketStream(V4)

    async def connect(host, port, timeout, *args):
        calls.append(timeout)
        if host == V6:
            clock[0] += timeout
            raise httpcore.ConnectTimeout(TOKEN)
        return stream

    monkeypatch.setattr(backend.backend, "connect_tcp", connect)
    assert await backend.connect_tcp("gitlab.example", 443, timeout=10) is stream
    assert calls == [5, 5] and sum(calls) == 10


@pytest.mark.parametrize("phase,error", [
    ("tls", httpcore.ConnectError), ("tls", httpcore.ConnectTimeout),
    ("send_headers", httpcore.WriteError), ("send_body", httpcore.WriteTimeout),
    ("response_headers", httpcore.ReadTimeout), ("response_headers", httpcore.RemoteProtocolError),
    ("response_body", httpcore.ReadError),
])
async def test_no_address_fallback_or_post_replay_after_tcp(
    monkeypatch, config, sockets, phase, error, caplog,
):
    calls, streams = sockets((V6, V4))
    original_write, original_read = SocketStream.write, SocketStream.read

    async def tls(self, *args, **kwargs):
        await self.aclose()  # AnyIOStream closes its socket on TLS failure.
        raise error(TOKEN)

    async def write(self, buffer, timeout=None):
        await original_write(self, buffer, timeout)
        if (phase == "send_headers" and buffer.startswith(b"POST ")) or (
            phase == "send_body" and buffer.startswith(b"{")):
            raise error(TOKEN)

    async def read(self, *args, **kwargs):
        if phase in {"send_headers", "response_headers"}:
            raise httpcore.ReadError(TOKEN) if phase == "send_headers" else error(TOKEN)
        if phase == "response_body":
            if not getattr(self, "headers_returned", False):
                self.headers_returned = True
                return b"HTTP/1.1 201 Created\r\nContent-Length: 999\r\n\r\n{"
            raise error(TOKEN)
        return await original_read(self, *args, **kwargs)

    if phase == "tls":
        monkeypatch.setattr(SocketStream, "start_tls", tls)
    monkeypatch.setattr(SocketStream, "write", write)
    monkeypatch.setattr(SocketStream, "read", read)
    with pytest.raises(GitLabError) as caught:
        await GitLabClient(config).launch(123, "branch-secret", uuid4())
    assert caught.value.ambiguous and len(calls) == 1
    assert len(streams) == 1 and streams[0].closed
    assert b"".join(streams[0].writes).count(b"POST ") <= 1
    assert any(e["stage"] == phase and e.get("method") == "POST" for e in events(caplog))
    assert TOKEN not in caplog.text and "branch-secret" not in caplog.text
    assert "COVERAGE_RUN_ID" not in caplog.text and config.coverage_url not in caplog.text


async def test_outer_deadline_while_waiting_for_response_is_ambiguous(
    monkeypatch, config, sockets, caplog,
):
    calls, streams = sockets((V6, V4))
    real_timeout = asyncio.timeout
    monkeypatch.setattr(gitlab_client.asyncio, "timeout", lambda _: real_timeout(0.05))

    async def read(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(SocketStream, "read", read)
    with pytest.raises(GitLabError) as caught:
        await GitLabClient(config).launch(123, "main", uuid4())
    assert caught.value.ambiguous and len(calls) == 1
    assert b"".join(streams[0].writes).count(b"POST ") == 1
    assert any(e["stage"] == "response_headers" and e["outcome"] == "timeout" for e in events(caplog))


async def test_peer_mismatch_fails_closed_without_trying_next_ip(monkeypatch, sockets):
    calls, streams = sockets((V6, V4))
    monkeypatch.setattr(SocketStream, "get_extra_info", lambda *args: ("127.0.0.1", 443))
    destination = await upstream.validate_endpoint("https://gitlab.example")
    async with httpx.AsyncClient(transport=PinnedTransport(destination)) as client:
        with pytest.raises(httpx.RequestError):
            await client.post(str(destination.url))
    assert len(calls) == 1 and streams[0].closed
    assert streams[0].writes == streams[0].tls == []


async def test_mixed_public_private_dns_never_connects(config, sockets, caplog):
    calls, streams = sockets((V6, V4, "10.0.0.1"))
    with pytest.raises(GitLabError) as caught:
        await GitLabClient(config).launch(123, "main", uuid4())
    assert caught.value.code == "gitlab_destination_unavailable" and not caught.value.ambiguous
    assert calls == streams == []
    assert any(e["stage"] == "policy" for e in events(caplog))


@pytest.mark.parametrize("error", [OSError, TimeoutError])
async def test_dns_failure_is_distinct_and_never_sends_credentials(
    monkeypatch, config, sockets, error, caplog,
):
    calls, streams = sockets((V4,))

    async def dns(*args):
        raise error(TOKEN)

    monkeypatch.setattr(upstream, "_resolve", dns)
    with pytest.raises(GitLabError) as caught:
        await GitLabClient(config).launch(123, "main", uuid4())
    assert not caught.value.ambiguous and calls == streams == []
    assert any(e["stage"] == "dns" and e["outcome"] == (
        "timeout" if error is TimeoutError else "error") for e in events(caplog))
    assert TOKEN not in caplog.text


async def test_cancellation_does_not_try_next_address(config, sockets):
    calls, _ = sockets((V6, V4), [asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await GitLabClient(config).launch(123, "main", uuid4())
    assert len(calls) == 1


async def test_untrusted_trace_extension_is_ignored(sockets):
    calls, _ = sockets((V4,))
    destination = await upstream.validate_endpoint("https://gitlab.example")

    async def malicious(*args):
        pytest.fail("Caller supplied callback executed")

    async with httpx.AsyncClient(transport=PinnedTransport(destination)) as client:
        await client.post(str(destination.url), extensions={"trace": malicious})
    assert len(calls) == 1


def test_diagnostics_reject_unallowlisted_strings_and_broken_sink(monkeypatch, caplog):
    outbound_event(TOKEN, "error")
    outbound_event("tcp", TOKEN)
    outbound_event("tcp", "error", method=TOKEN, status=TOKEN, elapsed_ms=TOKEN)
    assert events(caplog) == [{"event": "coverage_outbound", "stage": "tcp", "outcome": "error"}]
    from app import diagnostics

    def broken(*args):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(diagnostics._pending, "put_nowait", broken)
    outbound_event("tcp", "error")  # Best effort, no fallback traceback/output.
    assert TOKEN not in caplog.text


async def test_http_diagnostics_never_log_headers_bodies_or_pipeline_variables(
    config, gitlab_http, caplog, capfd,
):
    from dataclasses import replace
    secrets = [TOKEN, "cookie-secret", "authorization-secret", "body-secret", "variable-secret"]
    config = replace(config, coverage_url="https://coverage.example/variable-secret")
    requests = gitlab_http(lambda request: httpx.Response(
        503, headers={"Set-Cookie": "session=cookie-secret", "Authorization": "Bearer authorization-secret"},
        text="body-secret " + TOKEN,
    ))
    with pytest.raises(GitLabError) as caught:
        await GitLabClient(config).launch(123, "branch-secret", uuid4())
    assert len(requests) == 1 and caught.value.ambiguous
    assert any(e["stage"] == "http" and e["status"] == 503 for e in events(caplog))
    captured = capfd.readouterr()
    output = caplog.text + captured.out + captured.err + str(caught.value)
    assert all(secret not in output for secret in [*secrets, "branch-secret", "COVERAGE_RUN_ID"])
    assert all(set(e) <= {"event", "stage", "outcome", "method", "status", "elapsed_ms",
                          "attempt", "family", "run_id"} for e in events(caplog))


async def test_redirect_after_tcp_fallback_never_sends_post_to_redirect_target(
    monkeypatch, config, sockets,
):
    calls, streams = sockets((V6, V4), [httpcore.ConnectError(TOKEN)])

    async def redirect(self, *args, **kwargs):
        return (b"HTTP/1.1 307 Temporary Redirect\r\nContent-Length: 0\r\n"
                b"Location: http://169.254.169.254/secret\r\n\r\n")

    monkeypatch.setattr(SocketStream, "read", redirect)
    with pytest.raises(GitLabError) as caught:
        await GitLabClient(config).launch(123, "main", uuid4())
    assert not caught.value.ambiguous and caught.value.code == "gitlab_rejected"
    assert [c[0] for c in calls] == [V6, V4]
    assert b"".join(streams[0].writes).count(b"POST ") == 1
