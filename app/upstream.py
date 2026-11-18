"""Bounded outbound HTTP with DNS validation and numeric-address connections.

The HTTP origin remains unchanged for Host, TLS SNI and certificate verification.
Only the socket destination is pinned. No proxy environment, redirects or retries.
"""
import asyncio
import ipaddress
import os
import socket
import ssl
import time
from contextlib import contextmanager, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import anyio
from anyio.lowlevel import RunVar
from fastapi import HTTPException
import httpcore
import httpx

from app.diagnostics import outbound_event

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Development permits explicit HTTP origins, not arbitrary database/mail/admin protocols.
UNSAFE_PORTS = {1433, 1521, 2375, 2376, 3306, 5432, 5672, 6379, 9200, 11211, 27017}


class UnsafeEndpoint(ValueError):
    def __init__(self):
        super().__init__("Endpoint is not permitted by the outbound security policy")


class EndpointResolutionError(UnsafeEndpoint):
    """Resolution failed, preserving the base fail-closed outbound policy."""


class EndpointResolutionTimeout(EndpointResolutionError):
    pass


class UpstreamTooLarge(Exception):
    pass


def legacy_enabled() -> bool:
    return os.getenv("COVERAGE_LEGACY_PROXY", "false").lower() == "true"


def _url(value: str) -> httpx.URL:
    try:
        if (not value or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or any(c in value for c in "\\?#")):
            raise UnsafeEndpoint()
        parts = urlsplit(value)
        if "@" in parts.netloc or "%" in parts.netloc:
            raise UnsafeEndpoint()
        url = httpx.URL(value)
        if url.scheme not in {"https", "http"} or not url.host or url.host.endswith("."):
            raise UnsafeEndpoint()
        return url
    except (ValueError, httpx.InvalidURL):
        raise UnsafeEndpoint() from None


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    return url.scheme, url.host, url.port if url.port is not None else (443 if url.scheme == "https" else 80)


def _dev_allowed(url: httpx.URL) -> bool:
    if os.getenv("COVERAGE_DEV_MODE", "false").lower() != "true":
        return False
    # Exact origins only; no wildcard, suffix matching or environment-wide bypass.
    entries = os.getenv("COVERAGE_DEV_UPSTREAM_ORIGINS", "").split(",")
    for entry in entries:
        if entry.strip() and _origin(_url(entry.strip())) == _origin(url):
            return True
    return False


def parse_endpoint(value: str) -> httpx.URL:
    url = _url(value)
    port = _origin(url)[2]
    if (not 1 <= port <= 65535 or port in UNSAFE_PORTS or (port < 1024 and port not in {80, 443})
            or (not _dev_allowed(url) and (url.scheme != "https" or port != 443))):
        raise UnsafeEndpoint()
    return url


def _address(value: str, *, dev: bool) -> str:
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address):
            if address.ipv4_mapped:
                address = address.ipv4_mapped
            elif (address.sixtofour or address.teredo or address in ipaddress.ip_network("64:ff9b::/96")
                  or address in ipaddress.ip_network("64:ff9b:1::/48")):
                raise UnsafeEndpoint()
        # Metadata/link-local, multicast, unspecified and reserved destinations
        # remain forbidden even for explicitly allowed development origins.
        if (str(address) in {"168.63.129.16", "fd00:ec2::254"} or address.is_link_local or address.is_multicast or address.is_unspecified or (address.is_reserved and not (dev and address.is_loopback))
                or (not address.is_global and not (dev and (address.is_loopback or address.is_private)))):
            raise UnsafeEndpoint()
        return str(address)
    except ValueError:
        raise UnsafeEndpoint() from None


@dataclass(frozen=True)
class Destination:
    url: httpx.URL
    addresses: tuple[str, ...]


async def _resolve(host: str, port: int):
    return await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)


async def validate_endpoint(value: str) -> Destination:
    url = parse_endpoint(value)
    dev = _dev_allowed(url)
    try:
        ipaddress.ip_address(url.host)
    except ValueError:
        try:
            answers = await asyncio.wait_for(
                _resolve(url.host, _origin(url)[2]), 3.0,
            )
        except TimeoutError:
            raise EndpointResolutionTimeout() from None
        except OSError:
            raise EndpointResolutionError() from None
        if not answers or len(answers) > 32:
            raise UnsafeEndpoint()
        addresses = tuple(dict.fromkeys(_address(answer[4][0], dev=dev) for answer in answers))
    else:
        addresses = (_address(url.host, dev=dev),)
    return Destination(url, addresses)


class PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, destination: Destination):
        self.destination = destination
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self.destination.url.raw_host.decode("ascii") or port != _origin(self.destination.url)[2]:
            raise httpcore.ConnectError("Unapproved destination")
        # Only TCP establishment may try another already validated numeric IP.
        # No TLS or HTTP bytes have been sent here. Share one connect budget so
        # a blackholed first address cannot consume it before fallback is tried.
        started = time.monotonic()
        addresses = self.destination.addresses
        for index, address in enumerate(addresses):
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise httpcore.ConnectTimeout("TCP connect budget exhausted")
            attempt_timeout = None if remaining is None else remaining / (len(addresses) - index)
            try:
                stream = await self.backend.connect_tcp(address, port, attempt_timeout,
                                                        local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                outbound_event("tcp", "timeout" if isinstance(exc, httpcore.ConnectTimeout) else "error",
                               attempt=index + 1, family=ipaddress.ip_address(address).version,
                               elapsed_ms=int((time.monotonic() - started) * 1000))
                if index + 1 == len(addresses):
                    raise
                continue
            break
        else:
            raise httpcore.ConnectError("No approved addresses")
        peer = stream.get_extra_info("server_addr")
        try:
            if (not peer or ipaddress.ip_address(peer[0]) != ipaddress.ip_address(address) or peer[1] != port):
                raise ValueError
        except ValueError:
            await stream.aclose()
            raise httpcore.ConnectError("Unapproved peer") from None
        if index:
            outbound_event("tcp", "fallback_connected", attempt=index + 1,
                           family=ipaddress.ip_address(address).version,
                           elapsed_ms=int((time.monotonic() - started) * 1000))
        return stream

    async def connect_unix_socket(self, *args, **kwargs):
        raise httpcore.ConnectError("Unix sockets are not permitted")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


@contextmanager
def _httpx_errors():
    try:
        yield
    except httpcore.TimeoutException as exc:
        raise httpx.TimeoutException("Upstream timeout") from exc
    except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.ProxyError) as exc:
        raise httpx.RequestError("Upstream connection failed") from exc


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        with _httpx_errors():
            async for chunk in self.stream:
                yield chunk

    async def aclose(self):
        await self.stream.aclose()


class PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, destination: Destination, *, trace=None):
        self.destination = destination
        # Trusted instrumentation is supplied by application code, never by an
        # HTTP request extension (which could replace TLS identity or leak data).
        self.trace = trace
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=PinnedBackend(destination),
            max_connections=1, max_keepalive_connections=0, retries=0,
        )

    async def handle_async_request(self, request):
        if _origin(request.url) != _origin(self.destination.url):
            raise httpx.RequestError("Unapproved HTTP origin")
        with _httpx_errors():
            response = await self.pool.handle_async_request(httpcore.Request(
                method=request.method,
                url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                                 port=request.url.port, target=request.url.raw_path),
                headers=request.headers.raw, content=request.stream,
                # Do not accept caller-supplied TLS hostname or tracing callbacks.
                extensions={"timeout": request.extensions.get("timeout", {}),
                            **({"trace": self.trace} if self.trace is not None else {})},
            ))
        return httpx.Response(response.status, headers=response.headers,
                              stream=_ResponseStream(response.stream), extensions=response.extensions)

    async def aclose(self):
        await self.pool.aclose()


_slots = RunVar("coverage_outbound_slots")


@asynccontextmanager
async def outbound_slot():
    try:
        limiter = _slots.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(16)
        _slots.set(limiter)
    try:
        limiter.acquire_nowait()
    except anyio.WouldBlock:
        raise HTTPException(status_code=503, detail="Upstream capacity is temporarily exhausted") from None
    try:
        yield
    finally:
        limiter.release()
