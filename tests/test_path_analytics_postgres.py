"""PostgreSQL coverage for normalized path and relation analytics."""

import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.engine import make_url

from app.contracts import AnalysisResult, FieldReference, RequestObservationInput
from app.models import ObservedRequest
from app.path_analytics import get_run_paths
from app.services import create_run, create_service, import_sdl, record_observation
from test_multi_service_postgres import isolated_database  # noqa: F401


@pytest.fixture
async def postgres_session():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL missing; PostgreSQL integration NOT verified")
    configured = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://coverage:coverage_dev@localhost:5432/coverage"
    )
    if make_url(url).database == make_url(configured).database:
        pytest.fail("TEST_DATABASE_URL must name a database separate from DATABASE_URL")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                async with AsyncSession(
                    bind=connection, expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


def observation(run_id, fields, paths=(), max_depth=None):
    return RequestObservationInput(
        run_id=run_id,
        analysis=AnalysisResult(
            status="success", operation_type="query", fields=fields,
            paths=paths, max_depth=max_depth,
        ),
        http_method="POST", http_status=200,
        response_has_data=True, graphql_has_errors=False,
    )


@pytest.mark.integration
async def test_paths_depth_relations_filtering_and_pagination(postgres_session):
    session = postgres_session
    service = await create_service(session, "paths-" + uuid.uuid4().hex, "Path analytics")
    snapshot = await import_sdl(session, service.id, """
        type Query { user: User search: User }
        type User { name: String orders: [Order!]! }
        type Order { total: Int }
    """)
    run = await create_run(session, snapshot.id, "Path analytics", {})
    query_user = FieldReference("Query", "user")
    query_search = FieldReference("Query", "search")
    user_name = FieldReference("User", "name")
    user_orders = FieldReference("User", "orders")
    order_total = FieldReference("Order", "total")
    deep_path = (query_user, user_orders, order_total)
    short_path = (query_search, user_name)
    fields = (query_user, query_search, user_name, user_orders, order_total)

    # Repeated aliases or fragment occurrences in one operation count once per path.
    await record_observation(session, observation(
        run.id, fields, (deep_path, deep_path, short_path), max_depth=3,
    ))
    await record_observation(session, observation(
        run.id, fields, (deep_path, short_path), max_depth=3,
    ))

    result = await get_run_paths(session, run.id)
    assert result.path_data_availability == "available"
    assert result.max_depth == 3
    assert result.paths.total == 2
    rendered = {
        tuple((field.type_name, field.field_name) for field in item.path): item.path_hits
        for item in result.paths.items
    }
    assert rendered == {
        (("Query", "search"), ("User", "name")): 2,
        (("Query", "user"), ("User", "orders"), ("Order", "total")): 2,
    }
    relations = {
        (item.from_type, item.field_name, item.to_type): item.relation_hits
        for item in result.relations.items
    }
    assert relations == {
        ("Query", "search", "User"): 2,
        ("Query", "user", "User"): 2,
        ("User", "orders", "Order"): 2,
    }

    filtered = await get_run_paths(
        session, run.id, type_name="Order", field_name="total", limit=1,
    )
    assert filtered.paths.total == 1
    assert len(filtered.paths.items) == 1
    assert filtered.paths.items[0].path_hits == 2
    page = await get_run_paths(session, run.id, limit=1, offset=1)
    assert page.paths.total == 2
    assert len(page.paths.items) == 1


@pytest.mark.integration
async def test_legacy_requests_report_partial_or_unavailable_path_evidence(postgres_session):
    session = postgres_session
    service = await create_service(session, "paths-" + uuid.uuid4().hex, "Legacy paths")
    snapshot = await import_sdl(session, service.id, "type Query { health: String }")
    run = await create_run(session, snapshot.id, "Legacy paths", {})
    health = FieldReference("Query", "health")

    # Existing/manual successful observations have field evidence but no path evidence.
    await record_observation(session, observation(run.id, (health,)))
    partial = await get_run_paths(session, run.id)
    assert partial.path_data_availability == "unavailable"
    assert partial.max_depth is None
    assert partial.paths.total == 0

    await record_observation(session, observation(
        run.id, (health,), ((health,),), max_depth=1,
    ))
    mixed = await get_run_paths(session, run.id)
    assert mixed.path_data_availability == "partial"
    assert mixed.max_depth == 1
    assert mixed.paths.total == 1


@pytest.mark.integration
async def test_path_limit_preserves_selected_hits_and_response_evidence(postgres_session, monkeypatch):
    from app import query_analyzer
    from app.analyzer_adapter import analyze_e2e_query
    from app.e2e_capture import capture_e2e_exchange
    from app.services import get_run_graph
    from sqlalchemy import select
    from app.models import RequestFieldEvidence
    service = await create_service(postgres_session, 'limit-' + uuid.uuid4().hex, 'Limit')
    schema = await import_sdl(postgres_session, service.id, 'type Query { user: User } type User { id: ID }')
    run = await create_run(postgres_session, schema.id, 'Limit', {})
    monkeypatch.setattr(query_analyzer, 'MAX_PATHS_PER_REQUEST', 1)
    await capture_e2e_exchange(postgres_session, run_id=run.id,
        request_body=b'{"query":"{ user { id } }"}',
        decoded_response_body=b'{"data":{"user":{"id":"1"}}}',
        http_status=200, duration_ms=1, analyzer=analyze_e2e_query)
    graph = await get_run_graph(postgres_session, run.id)
    assert graph.summary.covered_fields == 2
    assert all(field.hits == 1 for type_ in graph.types for field in type_.fields)
    states = (await postgres_session.scalars(select(RequestFieldEvidence.state).join(
        ObservedRequest, ObservedRequest.id == RequestFieldEvidence.request_id).where(ObservedRequest.run_id == run.id))).all()
    assert states == ['observed', 'observed']
    from app.evidence_analytics import EvidenceFilters, get_field_evidence
    evidence = await get_field_evidence(postgres_session, run.id, EvidenceFilters())
    assert len(evidence.fields) == 2
    assert all(field.selected_hits == field.observed_hits == 1 for field in evidence.fields)
    assert evidence.selection_unavailable_request_count == evidence.evidence_unavailable_request_count == 0
    analytics = await get_run_paths(postgres_session, run.id)
    assert analytics.path_data_availability == 'unavailable'
    assert analytics.max_depth is None and analytics.paths.total == 0


@pytest.mark.integration
async def test_5000_paths_are_batched_deduplicated_and_safe_under_concurrent_writes(isolated_database, record_property):
    import asyncio
    from sqlalchemy import event, func, select
    from app.models import QueryPath, QueryPathField, RequestPathUsage
    size = 5000
    fields = tuple(FieldReference('Query', f'f{i}') for i in range(size))
    async with isolated_database() as session:
        service = await create_service(session, 'bulk-' + uuid.uuid4().hex, 'Bulk paths')
        schema = await import_sdl(session, service.id,
            'type Query { ' + ' '.join(f'f{i}: String' for i in range(size)) + ' }')
        run = await create_run(session, schema.id, 'Bulk paths', {})
    item = observation(run.id, fields, tuple((field,) for field in fields), max_depth=1)
    calls = []
    engine = isolated_database.kw['bind']
    def count(conn, cursor, statement, parameters, context, executemany):
        calls.append(statement)
    event.listen(engine.sync_engine, 'before_cursor_execute', count)
    try:
        async def write():
            async with isolated_database() as session:
                return await record_observation(session, item)
        await asyncio.wait_for(write(), 30)
        inserts = sum(sql.startswith('INSERT INTO query_paths ') for sql in calls)
        record_property('query_path_insert_calls', inserts)
        assert inserts == 5, f'{inserts} path INSERT round trips for {size} paths'
        assert len(calls) < 100
        calls.clear()
        # The run lock serializes capture, while the unique constraint and
        # ON CONFLICT retain a single dictionary entry per snapshot/path.
        await asyncio.wait_for(asyncio.gather(write(), write()), 30)
        assert sum(sql.startswith('INSERT INTO query_paths ') for sql in calls) == 10
        async with isolated_database() as session:
            assert await session.scalar(select(func.count()).select_from(QueryPath)) == size
            assert await session.scalar(select(func.count()).select_from(QueryPathField)) == size
            assert await session.scalar(select(func.count()).select_from(RequestPathUsage)) == size * 3
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', count)
