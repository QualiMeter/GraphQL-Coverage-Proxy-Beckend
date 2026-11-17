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
