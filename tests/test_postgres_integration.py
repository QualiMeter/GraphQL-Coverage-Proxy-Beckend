"""Genuine database test. Uses a SAVEPOINT under an outer rollback-only transaction."""

import os
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.contracts import AnalysisResult, FieldReference, RequestObservationInput
from app.models import ObservedRequest, RequestFieldUsage
from app.services import (
    ConflictError, InvalidReferenceError, create_run, create_service, finish_run, get_run_graph,
    import_sdl, list_requests, record_observation,
)


@pytest.fixture
async def postgres_session():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL missing; PostgreSQL integration NOT verified")

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


@pytest.mark.integration
async def test_postgresql_records_real_coverage_without_duplicate_fields(postgres_session):
    session = postgres_session
    service = await create_service(session, "test-" + uuid.uuid4().hex, "Integration test")
    schema = await import_sdl(
        session, service.id,
        "type Query { users: [User!]! health: String! } "
        "type User { id: ID! name: String! email: String! }",
    )
    duplicate = await import_sdl(
        session, service.id,
        "type Query{users:[User!]! health:String!} "
        "type User{id:ID! name:String! email:String!}",
    )
    assert schema.id == duplicate.id
    run = await create_run(session, schema.id, "E2E", {})
    observation = RequestObservationInput(
        run_id=run.id,
        analysis=AnalysisResult(
            status="success",
            operation_type="query",
            operation_name="GetUsers",
            fields=(
                FieldReference("Query", "users"),
                FieldReference("User", "id"),
                FieldReference("User", "id"),
                FieldReference("User", "name"),
            ),
        ),
        http_method="POST",
        http_status=200,
        response_has_data=True,
        graphql_has_errors=False,
        duration_ms=7,
    )
    await record_observation(session, observation)
    graph = await get_run_graph(session, run.id)
    assert graph.summary.total_fields == 5
    assert graph.summary.covered_fields == 3
    assert graph.summary.coverage_percent == 60.0
    assert next(
        item for item in graph.types if item.name == "User"
    ).covered_fields == 2

    await record_observation(session, observation)
    graph = await get_run_graph(session, run.id)
    assert graph.summary.coverage_percent == 60.0
    assert next(
        item for item in next(
            t for t in graph.types if t.name == "User"
        ).fields if item.name == "id"
    ).hits == 2

    await finish_run(session, run.id)
    with pytest.raises(ConflictError):
        await record_observation(session, observation)

@pytest.mark.integration
async def test_coverage_summary_counts_only_object_fields(postgres_session):
    session = postgres_session
    service = await create_service(session, "test-" + uuid.uuid4().hex, "Coverage regression")
    schema = await import_sdl(
        session, service.id,
        """
        interface Node { id: ID! }
        type Query { user(id: ID!): User users: [User!]! }
        type Mutation { createUser(name: String!): User! }
        type User implements Node { id: ID! name: String! email: String orders: [Order!]! }
        type Order { id: ID! total: Float! }
        """,
    )
    run = await create_run(session, schema.id, "Coverage regression", {})

    async def record(operation_type, *fields):
        await record_observation(session, RequestObservationInput(
            run_id=run.id,
            analysis=AnalysisResult(
                status="success",
                operation_type=operation_type,
                operation_name=None,
                fields=tuple(FieldReference(*field) for field in fields),
            ),
            http_method="POST",
            http_status=200,
            response_has_data=True,
            graphql_has_errors=False,
            duration_ms=7,
        ))
        return await get_run_graph(session, run.id)

    query_fields = (
        ("Query", "user"), ("User", "id"), ("User", "name"),
        ("User", "orders"), ("Order", "total"),
    )
    graph = await record("query", *query_fields)
    assert (graph.summary.covered_fields, graph.summary.total_fields) == (5, 9)
    assert graph.summary.coverage_percent == 55.56

    graph = await record("mutation", ("Mutation", "createUser"), ("User", "id"), ("User", "name"))
    assert (graph.summary.covered_fields, graph.summary.total_fields) == (6, 9)
    assert graph.summary.coverage_percent == 66.67

    graph = await record("query", *query_fields, ("Order", "total"))
    assert (graph.summary.covered_fields, graph.summary.total_fields) == (6, 9)
    assert graph.summary.coverage_percent == 66.67
    hits = {(node.name, field.name): field.hits for node in graph.types for field in node.fields}
    assert hits == {
        ("Query", "user"): 2, ("Query", "users"): 0,
        ("Mutation", "createUser"): 1,
        ("User", "id"): 3, ("User", "name"): 3, ("User", "email"): 0,
        ("User", "orders"): 2, ("Order", "id"): 0, ("Order", "total"): 2,
        ("Node", "id"): 0,
    }

    # Inject an interface hit directly at the storage boundary.
    graph = await record("query", ("Node", "id"))
    assert (graph.summary.covered_fields, graph.summary.total_fields) == (6, 9)
    assert graph.summary.coverage_percent == 66.67
    assert next(node for node in graph.types if node.name == "Node").fields[0].hits == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    "status, has_data, has_errors, fields, expected_hits, rejected",
    [
        ("success", False, True, ("health",), 0, False),
        ("success", True, True, ("health",), 1, False),
        ("validation_error", True, True, (), 0, False),
        ("success", True, False, ("health", "missing"), 0, True),
    ],
    ids=["no-data", "partial-errors", "analysis-error", "unknown-field"],
)
async def test_observation_storage_and_coverage(
    postgres_session, status, has_data, has_errors, fields, expected_hits, rejected,
):
    session = postgres_session
    service = await create_service(session, "test-" + uuid.uuid4().hex, "Observation rules")
    schema = await import_sdl(session, service.id, "type Query { health: String }")
    run = await create_run(session, schema.id, "Observation rules", {})
    observation = RequestObservationInput(
        run_id=run.id,
        analysis=AnalysisResult(
            status=status,
            operation_type="query",
            operation_name=None,
            fields=tuple(FieldReference("Query", field) for field in fields),
        ),
        http_method="POST",
        http_status=200,
        response_has_data=has_data,
        graphql_has_errors=has_errors,
        duration_ms=7,
    )

    if rejected:
        with pytest.raises(InvalidReferenceError):
            await record_observation(session, observation)
    else:
        await record_observation(session, observation)

    requests = await list_requests(session, run.id, limit=10, offset=0)
    assert len(requests) == (0 if rejected else 1)
    if requests:
        assert requests[0].analysis_status == status
        assert requests[0].response_has_data is has_data
        assert requests[0].graphql_has_errors is has_errors

    graph = await get_run_graph(session, run.id)
    assert graph.summary.covered_fields == expected_hits
    query = next(node for node in graph.types if node.name == "Query")
    assert query.fields[0].hits == expected_hits


@pytest.mark.integration
async def test_observation_rolls_back_when_field_usage_insert_fails(postgres_session):
    session = postgres_session
    service = await create_service(session, "test-" + uuid.uuid4().hex, "Observation rollback")
    schema = await import_sdl(session, service.id, "type Query { health: String }")
    run = await create_run(session, schema.id, "Observation rollback", {})
    observation = RequestObservationInput(
        run_id=run.id,
        analysis=AnalysisResult(
            status="success",
            operation_type="query",
            operation_name=None,
            fields=(FieldReference("Query", "health"),),
        ),
        http_method="POST",
        http_status=200,
        response_has_data=True,
        graphql_has_errors=False,
        duration_ms=7,
    )

    # The constraint lives in the same savepoint as the failed write.
    usage_count = select(func.count()).select_from(RequestFieldUsage)
    previous_usage_count = await session.scalar(usage_count)
    await session.execute(text(
        "ALTER TABLE request_field_usage "
        "ADD CONSTRAINT test_reject_field_usage CHECK (false) NOT VALID"
    ))
    with pytest.raises(IntegrityError, match="test_reject_field_usage"):
        await record_observation(session, observation)
    await session.rollback()

    request_count = select(func.count()).select_from(ObservedRequest).where(
        ObservedRequest.run_id == run.id
    )
    assert await session.scalar(request_count) == 0
    assert await session.scalar(usage_count) == previous_usage_count

    await record_observation(session, observation)
    assert await session.scalar(request_count) == 1
    assert await session.scalar(usage_count) == previous_usage_count + 1
