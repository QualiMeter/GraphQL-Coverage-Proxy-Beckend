"""Exercise Alembic upgrade/downgrade with historical rows in an isolated DB."""

import asyncio
import os
from pathlib import Path
import subprocess
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


async def test_auth_migration_preserves_existing_services_snapshots_runs_and_observations():
    target = os.environ.get("TEST_DATABASE_URL")
    if not target:
        pytest.skip("TEST_DATABASE_URL missing; PostgreSQL migrations NOT verified")
    database = "coverage_auth_migration_" + uuid.uuid4().hex
    admin = create_async_engine(target, isolation_level="AUTOCOMMIT")
    url = make_url(target).set(database=database).render_as_string(hide_password=False)
    engine = None

    async def migrate(*arguments):
        env = {**os.environ, "DATABASE_URL": url}
        result = await asyncio.to_thread(subprocess.run, [str(Path(os.sys.executable).with_name("alembic")), *arguments],
                                         cwd=ROOT, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
        await migrate("upgrade", "20261114_visibility")
        engine = create_async_engine(url)
        service, snapshot, run, observation = [uuid.uuid4() for _ in range(4)]
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO graphql_services(id,key,name) VALUES (:id,'legacy','Legacy')"), {"id": service})
            await connection.execute(text("INSERT INTO schema_snapshots(id,service_id,schema_hash,schema_sdl,source) VALUES (:id,:service,:hash,'type Query { x: Int }','sdl')"),
                                     {"id": snapshot, "service": service, "hash": "a" * 64})
            await connection.execute(text("INSERT INTO test_runs(id,snapshot_id,name,status) VALUES (:id,:snapshot,'Historical','completed')"), {"id": run, "snapshot": snapshot})
            await connection.execute(text("INSERT INTO observed_requests(id,run_id,http_method,analysis_status,duration_ms) VALUES (:id,:run,'POST','success',42)"), {"id": observation, "run": run})

        async def history():
            async with engine.connect() as connection:
                return [await connection.scalar(text(f"SELECT jsonb_agg(to_jsonb(t) - 'endpoint_url' - 'description' - 'updated_at' - 'operation_key' - 'display_name' - 'safe_graphql_document' - 'document_availability') FROM {table} t"))
                        for table in ("graphql_services", "schema_snapshots", "test_runs", "observed_requests")]
        before = await history()
        await migrate("upgrade", "head")
        assert await history() == before
        async with engine.connect() as connection:
            # The historical row lacks identity; the new cache must stay null.
            assert await connection.scalar(text("SELECT operation_key FROM observed_requests WHERE id=:id"),
                                           {"id": observation}) is None
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT display_name FROM schema_snapshots")) is None
            assert await connection.scalar(text("SELECT safe_graphql_document FROM observed_requests")) is None
            assert await connection.scalar(text("SELECT document_availability FROM observed_requests")) == "unavailable"
            assert await connection.scalar(text("SELECT endpoint_url FROM graphql_services")) is None
            assert await connection.scalar(text("SELECT description FROM graphql_services")) is None
            assert await connection.scalar(text("SELECT updated_at = created_at FROM graphql_services")) is True
        await migrate("check")
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM auth_users")) == 0
            assert await connection.scalar(text("SELECT count(*) FROM auth_service_grants")) == 0
        await migrate("downgrade", "20261114_visibility")
        assert await history() == before
        await migrate("upgrade", "head")
        assert await history() == before
    finally:
        if engine:
            await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        await admin.dispose()
