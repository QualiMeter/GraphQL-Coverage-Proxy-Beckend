"""Swagger/OpenAPI must use the external URL prefix behind Nginx."""

import httpx

from app.main import app


async def test_swagger_uses_asgi_root_path_for_openapi_url():
    transport = httpx.ASGITransport(app=app, root_path="/coverage")
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy.example") as client:
        docs = await client.get("/docs")
        schema = await client.get("/openapi.json")

    assert docs.status_code == 200
    assert "/coverage/openapi.json" in docs.text
    assert schema.status_code == 200
    assert schema.json()["servers"][0]["url"] == "/coverage"
