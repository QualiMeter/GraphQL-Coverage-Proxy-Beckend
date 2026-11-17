"""Wide comparison keeps HTTP responsive and releases the sole DB connection."""
import asyncio
import threading

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import api, run_comparison
from app.database import get_session_factory
from app.main import app
from app.schema_cache import schema_from_sdl
from app.schema_work import MAX_SCHEMA_WORKERS
from app.services import create_run, create_service, finish_run, import_sdl
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration


async def test_wide_comparison_releases_snapshot_before_bounded_cpu_work(isolated_database, monkeypatch):
    async with isolated_database() as s:
        url = s.bind.url
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setitem(app.dependency_overrides, get_session_factory, lambda: sessions)
    # 45,001 OBJECT fields and twenty runs, with deliberately tiny pages.
    sdl = 'type Query { item: T0 }\n' + '\n'.join(
        f'type T{i} {{ ' + ' '.join(f'f{j}: Int' for j in range(90)) + ' }' for i in range(500))
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    full, all_loaded, release = asyncio.Event(), asyncio.Event(), threading.Event()
    active = peak = loaded = 0
    mutex = threading.Lock()
    original = run_comparison._build_run_comparison
    original_loader = api.load_run_comparison
    queries = []
    tasks = []
    try:
        async with sessions() as s:
            service = await create_service(s, 'wide', 'Wide', is_public=True)
            snapshot = await import_sdl(s, service.id, sdl)
            runs = []
            for i in range(20):
                run = await create_run(s, snapshot.id, f'Run {i}', {})
                await finish_run(s, run.id)
                runs.append(run.id)
        schema_from_sdl.cache_clear()
        def count_sql(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement)
        event.listen(engine.sync_engine, 'before_cursor_execute', count_sql)
        async def load(session, payload, principal):
            nonlocal loaded
            assert await session.scalar(text('SHOW transaction_isolation')) == 'repeatable read'
            assert await session.scalar(text('SHOW transaction_read_only')) == 'on'
            data = await original_loader(session, payload, principal)
            loaded += 1
            if loaded == 4:
                all_loaded.set()
            return data
        def build(data, payload):
            nonlocal active, peak
            assert threading.get_ident() != loop_thread
            with mutex:
                active += 1
                peak = max(peak, active)
                if active == MAX_SCHEMA_WORKERS:
                    loop.call_soon_threadsafe(full.set)
            try:
                assert release.wait(20), 'HTTP loop could not progress while comparison was computing'
                return original(data, payload)
            finally:
                with mutex:
                    active -= 1
        monkeypatch.setattr(api, 'load_run_comparison', load)
        monkeypatch.setattr(run_comparison, '_build_run_comparison', build)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://graphql.rebyte.fun') as client:
            body = {'runIds': [str(r) for r in runs], 'baselineRunId': str(runs[0]),
                    'fields': {'limit': 1}, 'types': {'limit': 1}}
            tasks = [asyncio.create_task(client.post('/api/runs/compare', json=body)) for _ in range(4)]
            await asyncio.wait_for(full.wait(), 15)
            await asyncio.wait_for(all_loaded.wait(), 15)
            # Loader finishes just before the API context manager returns its
            # connection; SELECT also proves queued workers have no DB lease.
            async with sessions() as s:
                assert await asyncio.wait_for(s.scalar(text('SELECT 1')), 2) == 1
            assert engine.sync_engine.pool.checkedout() == 0
            health = await asyncio.wait_for(client.get('/health'), 2)
            assert health.status_code == 200
            assert peak == MAX_SCHEMA_WORKERS
            # Five batched SELECTs per comparison; SHOW statements are test-only.
            assert sum(q.lstrip().startswith('SELECT') for q in queries) == 4 * 5 + 1
            assert sum(q.startswith('SET TRANSACTION') for q in queries) == 4
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 30)
        for response in responses:
            assert response.status_code == 200, response.text
            data = response.json()
            assert data['fields']['total'] == 45001 and len(data['fields']['items']) == 1
            assert data['types']['total'] == 501 and len(data['types']['items']) == 1
            assert data['fieldCategories']['neverCovered'] == 45001
            assert data['fieldCategories']['stillUncovered'] == 45001
            assert all(r['totalFields'] == 45001 and r['coveredFields'] == 0
                       and r['totalRequests'] == 0 and r['coveragePercent'] == 0 for r in data['runs'])
        assert peak <= MAX_SCHEMA_WORKERS
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
