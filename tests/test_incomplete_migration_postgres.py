"""Repair only historical records with direct evidence of incomplete collection."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text

from app.services import create_service, import_sdl
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration


async def test_incomplete_migration_backfill_and_roundtrip(isolated_database):
    sessions = isolated_database
    async with sessions() as session:
        url = session.bind.url.render_as_string(hide_password=False)
        service = await create_service(session, "migration", "Migration")
        snapshot = await import_sdl(session, service.id, "type Query { x: Int }")
    async def migrate(*args):
        result = await asyncio.to_thread(subprocess.run,
            [str(Path(sys.executable).with_name("alembic")), *args],
            env={**os.environ, "DATABASE_URL": url}, cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    await migrate("downgrade", "20261122_field_evidence")
    ids = [uuid.uuid4() for _ in range(5)]
    async with sessions() as s:
        for index, ident in enumerate(ids):
            await s.execute(text("""INSERT INTO test_runs(id,snapshot_id,name,status,finished_at)
                VALUES (:id,:snapshot,'historical','completed',now())"""),
                {"id": ident, "snapshot": snapshot.id})
            if index == 4:  # manual completed is never rewritten
                continue
            await s.execute(text("""INSERT INTO gitlab_pipelines
                (run_id,gitlab_host,project_id,ref,pipeline_status,error_code,created_at,
                 deadline_at,finished_at,next_check_at,reconcile_page,check_failures)
                VALUES (:id,'https://gitlab.example',1,'main',:status,:error,now(),now(),
                        CASE WHEN :finished THEN now() ELSE NULL END,now(),1,0)"""),
                {"id": ident, "status": "running" if index == 0 else "success",
                 "error": "tracking_deadline_exceeded" if index == 1 else None,
                 "finished": index != 0})
        observation = uuid.uuid4()
        await s.execute(text("""INSERT INTO observed_requests(id,run_id,http_method,analysis_status)
            VALUES (:id,:run,'POST','success')"""), {"id": observation, "run": ids[0]})
        await s.commit()
    await migrate("upgrade", "head")
    async with sessions() as s:
        statuses = dict((await s.execute(text("SELECT id,status FROM test_runs"))).all())
        assert [statuses[i] for i in ids] == ["failed", "failed", "completed", "completed", "completed"]
        flags = dict((await s.execute(text("SELECT run_id,collection_incomplete FROM gitlab_pipelines"))).all())
        assert [flags[i] for i in ids[:4]] == [True, True, False, False]
        assert await s.scalar(text("SELECT finished_at IS NULL FROM gitlab_pipelines WHERE run_id=:id"), {"id": ids[0]})
        assert await s.scalar(text("SELECT count(*) FROM observed_requests WHERE id=:id"), {"id": observation}) == 1
    await migrate("check")
    await migrate("downgrade", "20261122_field_evidence")
    async with sessions() as s:
        assert await s.scalar(text("SELECT status FROM test_runs WHERE id=:id"), {"id": ids[0]}) == "failed"
    await migrate("upgrade", "head")
    async with sessions() as s:
        assert await s.scalar(text("SELECT collection_incomplete FROM gitlab_pipelines WHERE run_id=:id"), {"id": ids[0]})
