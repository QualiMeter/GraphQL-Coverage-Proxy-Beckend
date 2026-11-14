"""Swagger/OpenAPI must use the external URL prefix behind Nginx."""

from pathlib import Path

import httpx

from app.main import app


def test_production_compose_allows_empty_root_path_for_dedicated_subdomain():
    content = (Path(__file__).resolve().parents[1] / "deploy/compose.production.yaml").read_text()
    assert "${API_ROOT_PATH-/coverage}" in content
    assert "${API_ROOT_PATH:-/coverage}" not in content


async def test_swagger_uses_asgi_root_path_for_openapi_url():
    transport = httpx.ASGITransport(app=app, root_path="/coverage")
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy.example") as client:
        docs = await client.get("/docs")
        schema = await client.get("/openapi.json")

    assert docs.status_code == 200
    assert "/coverage/openapi.json" in docs.text
    assert schema.status_code == 200
    assert schema.json()["servers"][0]["url"] == "/coverage"
