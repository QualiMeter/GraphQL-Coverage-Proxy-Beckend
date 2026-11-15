import asyncio
import threading
import uuid

import pytest
from sqlalchemy import delete, func, select

from app import services
from app.auth_models import ServiceGrant
from app.models import SchemaField, SchemaSnapshot, SchemaType
from test_service_dns_transactions_postgres import service_dns_app  # noqa: F401
from test_multi_service_postgres import isolated_database  # noqa: F401
from test_schema_work import large_sdl

pytestmark = pytest.mark.integration


def pause_parser(monkeypatch):
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    started, finished, release = asyncio.Event(), asyncio.Event(), threading.Event()
    original = services.parse_sdl

    def parser(sdl):
        assert threading.get_ident() != main_thread
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(15)
            return original(sdl)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(services, 'parse_sdl', parser)
    return started, finished, release


async def snapshot_count(ctx):
    async with ctx.sessions() as session:
        return await session.scalar(select(func.count()).select_from(SchemaSnapshot))


async def test_large_http_import_and_graph_leave_pool_free_and_loop_responsive(service_dns_app, monkeypatch):
    ctx = service_dns_app
    started, finished, release = pause_parser(monkeypatch)
    task = asyncio.create_task(ctx.client.post('/api/schemas/import',
        json={'serviceId': str(ctx.service.id), 'sdl': large_sdl()}, headers=ctx.cookie_headers))
    try:
        await asyncio.wait_for(started.wait(), 15)
        assert ctx.engine.pool.checkedout() == 0
        assert (await ctx.client.get('/health')).status_code == 200
        release.set()
        response = await task
        assert response.status_code == 201, response.text
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    schema_id = response.json()['id']
    async with ctx.sessions() as session:
        total = await session.scalar(select(func.count()).select_from(SchemaField).join(SchemaType, SchemaType.id == SchemaField.type_id).where(
            SchemaType.snapshot_id == schema_id))
        assert total == 30_000

    loop_thread = threading.get_ident()
    original = services.schema_metadata
    def metadata(sdl):
        assert threading.get_ident() != loop_thread, 'GraphQL schema built on event loop'
        assert ctx.engine.pool.checkedout() == 0
        return original(sdl)
    monkeypatch.setattr(services, 'schema_metadata', metadata)
    graph = await ctx.client.get(f'/api/schemas/{schema_id}/graph')
    assert graph.status_code == 200, graph.text[:300]
    assert graph.json()['summary'] == {'totalFields': 30_000, 'coveredFields': 0, 'coveragePercent': 0.0}
    assert ctx.engine.pool.checkedout() == 0


async def test_cancelled_parse_does_not_write_or_leak_connection(service_dns_app, monkeypatch):
    ctx = service_dns_app
    before = await snapshot_count(ctx)
    started, finished, release = pause_parser(monkeypatch)
    task = asyncio.create_task(ctx.client.post('/api/schemas/import',
        json={'serviceId': str(ctx.service.id), 'sdl': 'type Query { cancelled: Int }'}, headers=ctx.cookie_headers))
    try:
        await asyncio.wait_for(started.wait(), 15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ctx.engine.pool.checkedout() == 0
        release.set()
        await asyncio.wait_for(finished.wait(), 15)
        assert await snapshot_count(ctx) == before
        assert ctx.engine.pool.checkedout() == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_after_flush_rolls_back_all_schema_rows(service_dns_app, monkeypatch):
    ctx = service_dns_app
    before = await snapshot_count(ctx)
    flushed = asyncio.Event()
    original = services._save_parsed_schema
    async def save(*args, **kwargs):
        result = await original(*args, **kwargs)
        flushed.set()
        await asyncio.Event().wait()
        return result
    monkeypatch.setattr(services, '_save_parsed_schema', save)
    task = asyncio.create_task(ctx.client.post('/api/schemas/import',
        json={'serviceId': str(ctx.service.id), 'sdl': 'type Query { cancelled: Int }'}, headers=ctx.cookie_headers))
    try:
        await asyncio.wait_for(flushed.wait(), 15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await snapshot_count(ctx) == before
        assert ctx.engine.pool.checkedout() == 0
        async with ctx.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(SchemaField)) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_acl_revocation_during_parse_prevents_save(service_dns_app, monkeypatch):
    ctx = service_dns_app
    before = await snapshot_count(ctx)
    started, finished, release = pause_parser(monkeypatch)
    task = asyncio.create_task(ctx.client.post('/api/schemas/import',
        json={'serviceId': str(ctx.service.id), 'sdl': 'type Query { denied: Int }'}, headers=ctx.cookie_headers))
    try:
        await asyncio.wait_for(started.wait(), 15)
        async with ctx.sessions() as session:
            await session.execute(delete(ServiceGrant).where(ServiceGrant.user_id == ctx.user_id))
            await session.commit()
        release.set()
        assert (await task).status_code == 404
        assert await snapshot_count(ctx) == before
        assert ctx.engine.pool.checkedout() == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_public_sdl_errors_and_missing_service_stay_unchanged(service_dns_app):
    ctx = service_dns_app
    before = await snapshot_count(ctx)
    for sdl in ('type Query {', 'type Other { value: String }', large_sdl(50_001)):
        result = await ctx.client.post('/api/schemas/import',
            json={'serviceId': str(ctx.service.id), 'sdl': sdl}, headers=ctx.cookie_headers)
        assert result.status_code == 422, result.text
        assert result.json() == {'detail': 'Invalid or unsupported GraphQL schema'}
    missing = await ctx.client.post('/api/schemas/import',
        json={'serviceId': str(uuid.uuid4()), 'sdl': 'type Query {'}, headers=ctx.key_headers)
    assert missing.status_code == 404
    assert await snapshot_count(ctx) == before


async def test_stats_matches_graph_without_building_schema_or_graph_dtos(service_dns_app, monkeypatch):
    from app.contracts import AnalysisResult, FieldReference, RequestObservationInput
    from app.services import create_run, get_run_graph, import_sdl, record_observation
    from app.run_analytics import get_run_stats
    ctx = service_dns_app
    sdl = '''interface Node { id: ID! } type Query { node: Item unused: String }
             type Item implements Node { id: ID! value: String }
             union Result = Item'''
    async with ctx.sessions() as session:
        snapshot = await import_sdl(session, ctx.service.id, sdl)
        run = await create_run(session, snapshot.id, 'stats', {})
        for status, data in [('success', True), ('success', True), ('success', False), ('internal_error', True)]:
            await record_observation(session, RequestObservationInput(
                run_id=run.id, analysis=AnalysisResult(status=status, operation_type='query',
                    fields=(FieldReference('Query', 'node'), FieldReference('Item', 'id'),
                            FieldReference('Node', 'id')) if status == 'success' else ()),
                http_method='POST', http_status=200, response_has_data=data,
                graphql_has_errors=False,
            ))
        graph = await get_run_graph(session, run.id)
        before = await get_run_stats(session, run.id)
        assert graph.summary.model_dump() == {'total_fields': 4, 'covered_fields': 2, 'coverage_percent': 50.0}

    def forbidden(*args, **kwargs):
        raise AssertionError('/stats must not construct GraphQL schema or full graph DTOs')
    monkeypatch.setattr(services, 'schema_metadata', forbidden)
    monkeypatch.setattr(services, 'GraphField', forbidden)
    async with ctx.sessions() as session:
        after = await get_run_stats(session, run.id)
    assert after == before
    assert after.model_dump(include={'total_fields', 'covered_fields', 'coverage_percent'}) == graph.summary.model_dump()
    response = await ctx.client.get(f'/api/runs/{run.id}/stats')
    assert response.status_code == 200
    assert response.json() == before.model_dump(mode='json', by_alias=True)
