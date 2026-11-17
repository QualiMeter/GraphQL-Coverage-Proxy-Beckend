"""Real proxy captures must not borrow DB connections while queued for CPU."""
import asyncio
import threading
import time

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import e2e_capture, proxy, run_comparison
from app.analyzer_adapter import analyze_e2e_query
from app.database import get_session, get_session_factory
from app.contracts import GraphQLQueryInput
from app.main import app
from app.models import ObservedRequest, RequestFieldEvidence, SchemaSnapshot
from starlette.datastructures import State
from app.services import create_run, create_service, finish_run, import_sdl
from test_graphql_proxy import TEST_ADMIN_KEY, raw_response, upstream  # noqa: F401
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('cancel', [False, True])
async def test_comparisons_and_capture_have_independent_capacity_and_no_db_leases(
    isolated_database, monkeypatch, upstream, cancel,
):
    async with isolated_database() as s:
        url = s.bind.url
    engine = create_async_engine(url, pool_size=2, max_overflow=0, pool_timeout=10)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async def dependency():
        async with sessions() as s:
            yield s
    monkeypatch.setitem(app.dependency_overrides, get_session, dependency)
    monkeypatch.setitem(app.dependency_overrides, proxy.get_proxy_session, dependency)
    monkeypatch.setitem(app.dependency_overrides, get_session_factory, lambda: sessions)
    monkeypatch.setenv('COVERAGE_API_KEY', TEST_ADMIN_KEY)
    monkeypatch.setattr(app, 'state', State({'coverage_analyzer': analyze_e2e_query}))
    loop = asyncio.get_running_loop()
    comparisons_full, captures_queued = asyncio.Event(), asyncio.Event()
    compare_release, capture_release = threading.Event(), threading.Event()
    mutex = threading.Lock()
    comparisons_entered = capture_workers = peak_capture = queued = 0
    build_original = run_comparison._build_run_comparison
    envelope_original = e2e_capture.response_envelope
    work_original = e2e_capture.schema_work
    comparisons, captures = [], []
    def build(data, payload):
        nonlocal comparisons_entered
        with mutex:
            comparisons_entered += 1
            if comparisons_entered == 2:
                loop.call_soon_threadsafe(comparisons_full.set)
        assert compare_release.wait(30)
        return build_original(data, payload)
    def envelope(body):
        nonlocal capture_workers, peak_capture
        with mutex:
            capture_workers += 1
            peak_capture = max(peak_capture, capture_workers)
        try:
            assert capture_release.wait(30)
            return envelope_original(body)
        finally:
            with mutex:
                capture_workers -= 1
    async def work(fn, *args):
        nonlocal queued
        if fn is envelope:
            queued += 1
            if queued == 4:
                captures_queued.set()
        return await work_original(fn, *args)
    monkeypatch.setattr(run_comparison, '_build_run_comparison', build)
    monkeypatch.setattr(e2e_capture, 'response_envelope', envelope)
    monkeypatch.setattr(e2e_capture, 'schema_work', work)
    wire = b'{"data":{"item":{"f0":1}},"errors":[{"message":"private","path":["item","f1"]}]}'
    requests = upstream(lambda request: raw_response(body=wire))
    try:
        sdl = 'type Query { item: T0 }\n' + '\n'.join(
            f'type T{i} {{ ' + ' '.join(f'f{j}: Int' for j in range(90)) + ' }' for i in range(500))
        async with sessions() as s:
            service = await create_service(s, 'contention', 'Contention', is_public=True,
                                           endpoint_url='https://upstream.example/graphql')
            snapshot = await import_sdl(s, service.id, sdl)
            runs = []
            for i in range(20):
                run = await create_run(s, snapshot.id, f'Completed {i}', {})
                await finish_run(s, run.id)
                runs.append(run.id)
            active = await create_run(s, snapshot.id, 'Capture', {})
            stored_sdl = (await s.get(SchemaSnapshot, snapshot.id)).schema_sdl
        await analyze_e2e_query(GraphQLQueryInput('{ item { f0 f1 } }'), stored_sdl)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url='https://graphql.rebyte.fun',
                                    headers={'X-Coverage-Api-Key': TEST_ADMIN_KEY}) as client:
            body = {'runIds': [str(r) for r in runs], 'baselineRunId': str(runs[0]),
                    'fields': {'limit': 1}, 'types': {'limit': 1}}
            comparisons = [asyncio.create_task(client.post('/api/runs/compare', json=body)) for _ in range(2)]
            await asyncio.wait_for(comparisons_full.wait(), 15)
            def capture():
                return asyncio.create_task(client.post('/graphql', json={'query': '{ item { f0 f1 } }'},
                    headers={'X-Coverage-Run-Id': str(active.id)}))
            started = time.perf_counter()
            captures = [capture() for _ in range(4)]
            try:
                await asyncio.wait_for(captures_queued.wait(), 5)
            except TimeoutError:
                pytest.fail(f'Only {queued}/4 captures reached CPU queue; '
                            f'{engine.pool.checkedout()}/2 DB connections retained by queued captures')
            queued_seconds = time.perf_counter() - started
            # All capture reads are finished; CPU workers and their queue remain paused.
            assert engine.pool.checkedout() == 0
            health_start = time.perf_counter()
            assert (await asyncio.wait_for(client.get('/health'), 2)).status_code == 200
            health_seconds = time.perf_counter() - health_start
            api_start = time.perf_counter()
            stats = await asyncio.wait_for(client.get(f'/api/runs/{active.id}/stats'), 2)
            api_seconds = time.perf_counter() - api_start
            assert stats.status_code == 200 and stats.json()['totalRequests'] == 0
            if cancel:
                for task in captures + comparisons:
                    task.cancel()
                await asyncio.gather(*captures, *comparisons, return_exceptions=True)
                assert engine.pool.checkedout() == 0
                assert comparisons_entered == 2  # shielded threads are still running
                fresh = capture()
                captures.append(fresh)
                capture_release.set()
                response = await asyncio.wait_for(fresh, 10)
                assert response.status_code == 200, response.text
                expected = 1
            else:
                capture_release.set()
                responses = await asyncio.wait_for(asyncio.gather(*captures), 10)
                assert all(r.status_code == 200 and r.content == wire for r in responses)
                expected = 4
            assert not compare_release.is_set(), 'Capture waited for comparison workers'
            assert peak_capture <= 2
            async with sessions() as s:
                observations = (await s.scalars(select(ObservedRequest).where(ObservedRequest.run_id == active.id))).all()
                assert len(observations) == expected
                assert all(r.analysis_status == 'success' and r.graphql_error_count == 1 for r in observations)
                counts = dict((await s.execute(select(RequestFieldEvidence.state, func.count())
                    .join(ObservedRequest, ObservedRequest.id == RequestFieldEvidence.request_id)
                    .where(ObservedRequest.run_id == active.id).group_by(RequestFieldEvidence.state))).all())
                assert counts == {'observed': 2 * expected, 'indeterminate': expected}
            assert len(requests) == (5 if cancel else 4), 'Upstream exchange was duplicated'
            compare_release.set()
            if not cancel:
                for result in await asyncio.wait_for(asyncio.gather(*comparisons), 20):
                    assert result.status_code == 200, result.text
                    assert result.json()['fieldCategories']['neverCovered'] == 45001
            # Probe after cancelled/running work: no lost worker slots or pool leases.
            result = await asyncio.wait_for(client.post('/api/runs/compare', json=body), 20)
            assert result.status_code == 200
            assert engine.pool.checkedout() == 0
            print({'cancel': cancel, 'queued_seconds': queued_seconds, 'health_seconds': health_seconds,
                   'stats_seconds': api_seconds, 'peak_capture_workers': peak_capture,
                   'observations': expected, 'pool_checkedout': engine.pool.checkedout()})
    finally:
        capture_release.set()
        compare_release.set()
        for task in captures + comparisons:
            if not task.done():
                task.cancel()
        await asyncio.gather(*captures, *comparisons, return_exceptions=True)
        await engine.dispose()
