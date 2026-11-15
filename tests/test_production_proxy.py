from pathlib import Path

import httpx
import pytest

from app.server import server_config, trusted_proxy_ips

PEER = "172.30.8.1"
ORIGIN = "https://graphql.rebyte.fun"
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", ["", "*", "172.16.0.0/12", "0.0.0.0/0", "nginx", "127.0.0.1,", "0.0.0.0", "::", "224.0.0.1"])
def test_production_refuses_missing_or_broad_proxy_trust(value, monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", value)
    with pytest.raises(ValueError):
        trusted_proxy_ips()


def test_proxy_trust_accepts_only_explicit_peers(monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", f"{PEER}, ::1, {PEER}")
    assert trusted_proxy_ips() == f"{PEER},::1"


@pytest.mark.parametrize("peer,forwarded,scheme,address", [
    (PEER, {"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.7"}, "https", "198.51.100.7"),
    ("172.30.8.9", {"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.7"}, "http", "172.30.8.9"),
    (PEER, {"X-Forwarded-Proto": "https,http"}, "http", PEER),
    (PEER, {"Forwarded": "proto=https;for=198.51.100.7", "X-Forwarded-Host": "attacker.example"}, "http", PEER),
])
async def test_uvicorn_uses_only_configured_transport_peer(peer, forwarded, scheme, address, monkeypatch):
    from fastapi import FastAPI, Request
    probe = FastAPI()

    @probe.get("/")
    async def inspect_request(request: Request):
        return {"scheme": request.url.scheme, "peer": request.client.host, "host": request.url.hostname}

    monkeypatch.setenv("FORWARDED_ALLOW_IPS", PEER)
    config = server_config()
    config.app = probe
    config.load()
    assert config.proxy_headers is True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=config.loaded_app, client=(peer, 32100)),
                                base_url="http://graphql.rebyte.fun") as client:
        result = (await client.get("/", headers=forwarded)).json()
    assert result == {"scheme": scheme, "peer": address, "host": "graphql.rebyte.fun"}


@pytest.mark.parametrize("peer,forwarded", [
    ("172.30.8.9", {"X-Forwarded-Proto": "https"}),
    (PEER, {}),
    (PEER, {"X-Forwarded-Proto": "http"}),
    (PEER, {"Forwarded": "proto=https"}),
])
async def test_spoofed_or_missing_https_cannot_authorize_browser_login(peer, forwarded, monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", PEER)
    monkeypatch.delenv("COVERAGE_DEV_MODE", raising=False)
    monkeypatch.setenv("COVERAGE_PUBLIC_ORIGIN", ORIGIN)
    config = server_config()
    config.load()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=config.loaded_app, client=(peer, 32100)),
                                base_url="http://graphql.rebyte.fun") as client:
        response = await client.post("/api/auth/login", json={"username": "viewer", "password": "test-password"},
                                     headers={"Origin": ORIGIN, **forwarded})
    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_deployment_wires_explicit_trust_and_validates_before_migration():
    content = (ROOT / "deploy/compose.production.yaml").read_text()
    assert "FORWARDED_ALLOW_IPS: ${FORWARDED_ALLOW_IPS:?" in content
    assert 'COVERAGE_DEV_MODE: "false"' in content
    assert "COVERAGE_PUBLIC_ORIGIN:" in content
    assert "127.0.0.1:${API_PORT:-5081}:8000" in content
    assert "python -m app.server --check-config && alembic upgrade head && exec python -m app.server" in content
