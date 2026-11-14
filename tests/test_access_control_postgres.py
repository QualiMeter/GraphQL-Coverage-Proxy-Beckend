"""Visibility is enforced for nested schema, run and coverage IDs in PostgreSQL."""

import uuid

import httpx
import pytest

from app.database import get_session
from app.main import app
from app.services import create_run, create_service, import_sdl
from test_postgres_integration import postgres_session  # noqa: F401


TEST_KEY = "test-only-coverage-admin-key-" + "x" * 40
pytestmark = pytest.mark.integration


async def test_private_is_default_and_nested_reads_cannot_bypass_visibility(
    postgres_session, monkeypatch
):
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
    session = postgres_session
    private = await create_service(session, "private-" + uuid.uuid4().hex, "Private")
    published = await create_service(session, "public-" + uuid.uuid4().hex, "Public")
    private_schema = await import_sdl(session, private.id, "type Query { hidden: String }")
    public_schema = await import_sdl(session, published.id, "type Query { visible: String }")
    private_run = await create_run(session, private_schema.id, "Private run", {})
    public_run = await create_run(
        session, public_schema.id, "Public run", {"internalToken": "must-not-be-public"}
    )

    async def session_override():
        yield session

    app.dependency_overrides[get_session] = session_override
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            admin_headers = {"X-Coverage-Api-Key": TEST_KEY}

            # All pre-existing and newly created services remain private by default.
            assert private.is_public is False
            assert published.is_public is False
            assert (await client.get("/api/services")).json() == []
            admin_list = await client.get("/api/services", headers=admin_headers)
            assert {row["id"] for row in admin_list.json()} == {str(private.id), str(published.id)}

            # Unauthorized publishing cannot alter visibility.
            response = await client.patch(
                f"/api/services/{published.id}", json={"isPublic": True}
            )
            assert response.status_code == 401
            response = await client.patch(
                f"/api/services/{published.id}", json={"isPublic": True}, headers=admin_headers
            )
            assert response.status_code == 200
            assert response.json()["isPublic"] is True

            public_list = await client.get("/api/services")
            assert [row["id"] for row in public_list.json()] == [str(published.id)]

            checks = [
                (f"/api/schemas?serviceId={private.id}", f"/api/schemas?serviceId={published.id}"),
                (f"/api/schemas/{private_schema.id}/graph", f"/api/schemas/{public_schema.id}/graph"),
                (f"/api/runs?snapshotId={private_schema.id}", f"/api/runs?snapshotId={public_schema.id}"),
                (f"/api/runs/{private_run.id}/coverage", f"/api/runs/{public_run.id}/coverage"),
            ]
            for private_path, public_path in checks:
                assert (await client.get(private_path)).status_code == 404
                assert (await client.get(private_path, headers=admin_headers)).status_code == 200
                assert (await client.get(public_path)).status_code == 200

            public_runs = await client.get(f"/api/runs?snapshotId={public_schema.id}")
            assert public_runs.json()[0]["metadata"] == {}
            admin_runs = await client.get(
                f"/api/runs?snapshotId={public_schema.id}", headers=admin_headers
            )
            assert admin_runs.json()[0]["metadata"] == {"internalToken": "must-not-be-public"}

            # Even request metadata for a publicly visible run is admin-only.
            journal = f"/api/runs/{public_run.id}/requests"
            assert (await client.get(journal)).status_code == 401
            assert (await client.get(journal, headers=admin_headers)).status_code == 200

            # Revoke publication without deleting schemas or run history.
            response = await client.patch(
                f"/api/services/{published.id}", json={"isPublic": False}, headers=admin_headers
            )
            assert response.status_code == 200
            assert (await client.get("/api/services")).json() == []
            assert (await client.get(f"/api/runs/{public_run.id}/coverage")).status_code == 404
    finally:
        app.dependency_overrides.pop(get_session, None)

