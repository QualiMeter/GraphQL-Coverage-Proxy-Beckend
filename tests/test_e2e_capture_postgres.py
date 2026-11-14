"""Real DB checks for the E2E exchange adapter (no fake coverage in production)."""

import hashlib
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.engine import make_url

from app.contracts import AnalysisResult, FieldReference
from app.e2e_capture import capture_e2e_exchange
from app.models import ObservedRequest
from app.services import (
    ConflictError, NotFoundError, create_run, create_service, finish_run,
    get_run_graph, import_sdl,
)


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
                    bind=connection,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_capture_persists_real_run_observation_and_only_analyzed_fields(postgres_session):
    session = postgres_session
    service = await create_service(
        session, "e2e-" + uuid.uuid4().hex, "E2E capture integration test"
    )
    snapshot = await import_sdl(
        session, service.id, "type Query { user: User } type User { id: ID! name: String! }"
    )
    run = await create_run(session, snapshot.id, "Real E2E run", {})
    query = "query GetUser { user { id } }"
    body = (
        '{"query":"query GetUser { user { id } }",'
        '"operationName":"GetUser","variables":{"token":"private-value"}}'
    ).encode()
    analyzed: list[str] = []

    async def analyzer(gql_input, schema_sdl):
        analyzed.append(gql_input.query)
        assert gql_input.variables["token"] == "private-value"
        assert "type Query" in schema_sdl
        return AnalysisResult(
            status="success",
            operation_type="query",
            operation_name="GetUser",
            fields=(FieldReference("Query", "user"), FieldReference("User", "id")),
        )

    observation_id = await capture_e2e_exchange(
        session,
        run_id=run.id,
        request_body=body,
        decoded_response_body=b'{"data":{"user":{"id":"1"}}}',
        http_status=200,
        duration_ms=24,
        analyzer=analyzer,
    )
    row = await session.get(ObservedRequest, observation_id)
    assert row is not None
    assert row.run_id == run.id
    assert row.operation_name == "GetUser"
    assert row.query_hash == hashlib.sha256(query.encode()).hexdigest()
    assert row.http_status == 200
    assert row.duration_ms == 24
    assert row.response_has_data is True
    assert row.graphql_has_errors is False
    assert row.analysis_status == "success"
    assert analyzed == [query]
    assert "private-value" not in repr(row)
    graph = await get_run_graph(session, run.id)
    assert (graph.summary.total_fields, graph.summary.covered_fields) == (3, 2)

    # Unsupported request shapes must never be assigned fake covered fields.
    async def never_analyze(*_args):
        pytest.fail("Unsupported request must not reach the AST analyzer")

    unsupported_id = await capture_e2e_exchange(
        session,
        run_id=run.id,
        request_body=b'[{"query":"{ user { id } }"}]',
        decoded_response_body=b'{"data":null,"errors":[{"message":"private"}]}',
        http_status=400,
        duration_ms=5,
        analyzer=never_analyze,
    )
    unsupported = await session.get(ObservedRequest, unsupported_id)
    assert unsupported.analysis_status == "unsupported"
    assert unsupported.query_hash is None
    assert unsupported.response_has_data is True
    assert unsupported.graphql_has_errors is True
    graph = await get_run_graph(session, run.id)
    assert graph.summary.covered_fields == 2

    # Broken analyzers get a stable error code, not fabricated fields or secrets.
    async def broken_analyzer(*_args):
        raise RuntimeError("private-value")

    broken_id = await capture_e2e_exchange(
        session,
        run_id=run.id,
        request_body=body,
        decoded_response_body=None,
        http_status=None,
        duration_ms=None,
        analyzer=broken_analyzer,
    )
    broken = await session.get(ObservedRequest, broken_id)
    assert broken.analysis_status == "internal_error"
    assert broken.error_code == "analyzer_failure"
    assert broken.response_has_data is None
    assert broken.graphql_has_errors is None
    assert broken.query_hash == hashlib.sha256(query.encode()).hexdigest()

    await finish_run(session, run.id)
    with pytest.raises(ConflictError):
        await capture_e2e_exchange(
            session,
            run_id=run.id,
            request_body=body,
            decoded_response_body=b'{"data":{}}',
            http_status=200,
            duration_ms=1,
            analyzer=analyzer,
        )
    with pytest.raises(NotFoundError):
        await capture_e2e_exchange(
            session,
            run_id=uuid.uuid4(),
            request_body=body,
            decoded_response_body=b'{"data":{}}',
            http_status=200,
            duration_ms=1,
            analyzer=analyzer,
        )
