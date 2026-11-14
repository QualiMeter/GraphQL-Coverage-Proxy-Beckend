"""Aggregates and access are verified against real PostgreSQL observations."""

import hashlib
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text

from app.contracts import AnalysisResult, FieldReference, RequestObservationInput
from app.analyzer_adapter import analyze_e2e_query
from app.database import get_session
from app.main import app
from app.e2e_capture import capture_e2e_exchange
from app.models import ObservedRequest
from app.run_analytics import get_run_stats, list_operations, operation_key
from app.services import (
    NotFoundError, create_run, create_service, finish_run, get_run_graph, import_sdl, record_observation,
)
from test_e2e_capture_postgres import postgres_session  # noqa: F401


pytestmark = pytest.mark.integration
TEST_KEY = "test-only-coverage-admin-key-" + "x" * 40
SDL = """
interface Node { id: ID! }
type Query { users: [User!]! health: String! unused: String }
type User implements Node { id: ID! name: String! }
type Mutation { update: Boolean }
type Subscription { changed: String }
"""


@pytest.fixture
async def analytics_run(postgres_session):
    service = await create_service(postgres_session, "analytics-" + uuid.uuid4().hex, "Analytics")
    schema = await import_sdl(postgres_session, service.id, SDL)
    run = await create_run(postgres_session, schema.id, "Analytics run", {})
    return service, schema, run


async def observe(
    session, run_id, *, query_hash="a" * 64, name="GetUsers", kind="query", status="success",
    fields=(), data=True, http=200, errors=False, duration=None, at="2026-11-14T00:02:00+00:00",
):
    request_id = await record_observation(session, RequestObservationInput(
        run_id=run_id, query_hash=query_hash,
        analysis=AnalysisResult(
            status=status, operation_type=kind, operation_name=name,
            fields=tuple(FieldReference(*coordinate) for coordinate in fields),
        ),
        http_method="POST", http_status=http, response_has_data=data,
        graphql_has_errors=errors, duration_ms=duration,
    ))
    request = await session.get(ObservedRequest, request_id)
    request.created_at = datetime.fromisoformat(at)
    await session.commit()


@pytest.mark.parametrize("name", ["GetUsers", None])
async def test_empty_run_and_unknown_duration(postgres_session, analytics_run, name):
    _, _, run = analytics_run
    session = postgres_session
    stats = await get_run_stats(session, run.id)
    assert stats.model_dump() == {
        "total_requests": 0, "counted_requests": 0, "unique_operations": 0,
        "query_requests": 0, "mutation_requests": 0, "subscription_requests": 0,
        "http_error_count": 0, "graphql_error_count": 0, "avg_duration_ms": None,
        "total_fields": 7, "covered_fields": 0, "coverage_percent": 0.0, "request_timeline": [],
    }
    assert await list_operations(session, run.id) == []
    # Repeated schema coordinates still produce one request-field association.
    for _ in range(2):
        await observe(session, run.id, name=name, fields=(("Query", "health"), ("Query", "health")))
    stats = await get_run_stats(session, run.id)
    assert stats.avg_duration_ms is None
    assert stats.request_timeline[0].avg_duration_ms is None
    operation, = await list_operations(session, run.id)
    assert operation.avg_duration_ms is None
    assert operation.operation_name == name
    assert operation.request_count == operation.fields[0].request_count == 2
    assert stats.coverage_percent == (await get_run_graph(session, run.id)).summary.coverage_percent


async def test_aggregates_group_identity_errors_fields_timeline_and_history(postgres_session, analytics_run):
    _, schema, run = analytics_run
    session = postgres_session
    # A session with a fractional timezone offset must still produce UTC minute boundaries.
    await session.execute(text("SET LOCAL TIME ZONE 'Asia/Kathmandu'"))
    fields = (("Query", "users"), ("User", "id"), ("User", "id"), ("User", "name"))
    await observe(session, run.id, fields=fields, duration=10, at="2026-11-13T23:59:59.999999+00:00")
    await observe(session, run.id, fields=fields, duration=30, http=500, errors=True,
                  at="2026-11-14T05:45:00+05:45")
    await observe(session, run.id, fields=fields, data=False, http=400,
                  at="2026-11-14T00:00:59+00:00")
    await observe(session, run.id, query_hash="b" * 64, fields=(("Query", "health"),))
    await observe(session, run.id, query_hash="c" * 64, name=None, fields=(("Query", "health"),), duration=0)
    await observe(session, run.id, name="Other", fields=(("Query", "health"),))
    await observe(session, run.id, kind="mutation", fields=(("Mutation", "update"),), http=201, duration=20)
    await observe(session, run.id, query_hash="d" * 64, name=None, kind=None, status="parse_error",
                  data=False, http=400, errors=True)
    await observe(session, run.id, query_hash=None, name=None, kind=None, status="unsupported",
                  data=False, http=None, errors=None)
    await observe(session, run.id, query_hash="e" * 64, name="Updates", kind="subscription",
                  status="unsupported", data=False, errors=True)
    # Legacy observations without a hash count in stats and coverage, without fake identity.
    await observe(session, run.id, query_hash=None, fields=(("Query", "health"),))
    await observe(session, run.id, status="validation_error", data=False, errors=True)

    operations = await list_operations(session, run.id)
    assert len(operations) == 6
    a = next(item for item in operations if item.operation_key == operation_key("a" * 64, "GetUsers", "query"))
    assert (a.request_count, a.counted_request_count, a.http_error_count, a.graphql_error_count) == (4, 2, 2, 2)
    assert a.avg_duration_ms == 20.0
    assert a.last_seen_at == datetime(2026, 11, 14, 0, 2, tzinfo=timezone.utc)
    assert [(f.type_name, f.field_name, f.request_count) for f in a.fields] == [
        ("Query", "users", 3), ("User", "id", 3), ("User", "name", 3),
    ]
    assert len(await list_operations(session, run.id, operation_name="GetUsers")) == 3
    assert len(await list_operations(session, run.id, operation_type="mutation")) == 1
    assert len(await list_operations(session, run.id, operation_name="GetUsers", operation_type="query")) == 2
    assert await list_operations(session, run.id, operation_name="getusers") == []
    anonymous = next(item for item in operations if item.operation_name is None)
    assert anonymous.request_count == 1 and anonymous.avg_duration_ms == 0.0
    subscription = next(item for item in operations if item.operation_type == "subscription")
    assert subscription.fields == [] and subscription.counted_request_count == 0

    stats = await get_run_stats(session, run.id)
    assert (stats.total_requests, stats.counted_requests, stats.unique_operations) == (12, 7, 6)
    assert (stats.query_requests, stats.mutation_requests, stats.subscription_requests) == (8, 1, 1)
    assert (stats.http_error_count, stats.graphql_error_count) == (3, 4)
    assert stats.avg_duration_ms == 15.0
    graph = await get_run_graph(session, run.id)
    assert (stats.total_fields, stats.covered_fields, stats.coverage_percent) == (7, 5, 71.43)
    assert stats.model_dump(include={"total_fields", "covered_fields", "coverage_percent"}) == graph.summary.model_dump()
    assert [item.bucket_start.isoformat() for item in stats.request_timeline] == [
        "2026-11-13T23:59:00+00:00", "2026-11-14T00:00:00+00:00", "2026-11-14T00:02:00+00:00",
    ]
    assert [(b.request_count, b.counted_request_count, b.http_error_count, b.graphql_error_count, b.avg_duration_ms)
            for b in stats.request_timeline] == [(1, 1, 0, 0, 10.0), (2, 1, 2, 1, 30.0), (9, 5, 1, 3, 10.0)]

    await finish_run(session, run.id)
    later = await create_run(session, schema.id, "Later run", {})
    await observe(session, later.id, fields=(("Query", "unused"),), duration=999)
    new_schema = await import_sdl(session, schema.service_id, SDL.replace("unused: String", "unused: String added: ID"))
    await create_run(session, new_schema.id, "New schema", {})
    assert await get_run_stats(session, run.id) == stats
    assert await list_operations(session, run.id) == operations
    # Keys are stable across runs, independent of timestamps and database IDs.
    later_operation, = await list_operations(session, later.id)
    assert later_operation.operation_key == a.operation_key


async def test_failed_observations_have_no_fabricated_operations(postgres_session, analytics_run):
    _, _, run = analytics_run
    for query_hash in (None, "f" * 64):
        await observe(postgres_session, run.id, query_hash=query_hash, name=None, kind=None,
                      status="parse_error", data=False, errors=True)
    assert await list_operations(postgres_session, run.id) == []
    stats = await get_run_stats(postgres_session, run.id)
    assert stats.total_requests == 2 and stats.unique_operations == 0
    assert stats.counted_requests == stats.covered_fields == 0
    assert stats.query_requests == stats.mutation_requests == stats.subscription_requests == 0


async def test_analytics_visibility_http_filters_and_existing_protection(postgres_session, analytics_run, monkeypatch):
    service, _, run = analytics_run
    monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
    await observe(postgres_session, run.id, name=None, fields=(("Query", "health"),))

    async def session_override():
        yield postgres_session

    app.dependency_overrides[get_session] = session_override
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"X-Coverage-Api-Key": TEST_KEY}
            for endpoint in ("operations", "stats"):
                path = f"/api/runs/{run.id}/{endpoint}"
                for anonymous in ({}, {"X-Coverage-Api-Key": "bad"}):
                    assert (await client.get(path, headers=anonymous)).status_code == 404
                assert (await client.get(path, headers=headers)).status_code == 200
                assert (await client.get(f"/api/runs/{uuid.uuid4()}/{endpoint}", headers=headers)).status_code == 404
            response = await client.patch(f"/api/services/{service.id}", headers=headers, json={"isPublic": True})
            assert response.status_code == 200
            for endpoint in ("operations", "stats"):
                assert (await client.get(f"/api/runs/{run.id}/{endpoint}")).status_code == 200
            operations = (await client.get(f"/api/runs/{run.id}/operations")).json()
            assert operations[0]["operationName"] is None
            assert operations[0]["fields"] == [{"typeName": "Query", "fieldName": "health", "requestCount": 1}]
            filtered = await client.get(f"/api/runs/{run.id}/operations?operationType=mutation")
            assert filtered.status_code == 200 and filtered.json() == []
            assert (await client.get(f"/api/runs/{run.id}/operations?operationType=bogus")).status_code == 422
            assert (await client.get(f"/api/runs/{run.id}/requests")).status_code == 401
            assert (await client.get(f"/api/runs/{run.id}/requests", headers=headers)).status_code == 200
            assert (await client.post("/graphql", json={})).status_code == 401
            # Public reads still work when administrative auth is not configured.
            monkeypatch.delenv("COVERAGE_API_KEY")
            for endpoint in ("operations", "stats"):
                assert (await client.get(f"/api/runs/{run.id}/{endpoint}")).status_code == 200
            monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
            await client.patch(f"/api/services/{service.id}", headers=headers, json={"isPublic": False})
            for endpoint in ("operations", "stats"):
                assert (await client.get(f"/api/runs/{run.id}/{endpoint}")).status_code == 404
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_missing_runs(postgres_session):
    for aggregate in (list_operations, get_run_stats):
        with pytest.raises(NotFoundError):
            await aggregate(postgres_session, uuid.uuid4())


async def test_real_analyzer_never_invents_fields_for_parse_errors_or_subscriptions(postgres_session, analytics_run):
    _, _, run = analytics_run
    for request_body in (
        b'{"query":"query { users { id id } }"}',
        b'{"query":"query {"}',
        b'{"query":"subscription Updates { changed }"}',
        b'not-json',
    ):
        await capture_e2e_exchange(
            postgres_session, run_id=run.id, request_body=request_body,
            decoded_response_body=b'{"data":{},"errors":[{"message":"upstream failure"}]}',
            http_status=500, duration_ms=None, analyzer=analyze_e2e_query,
        )
    operations = await list_operations(postgres_session, run.id)
    assert len(operations) == 2
    query = next(item for item in operations if item.operation_type == "query")
    assert query.operation_name is None
    assert query.operation_key == operation_key(
        hashlib.sha256(b"query { users { id id } }").hexdigest(), None, "query"
    )
    assert [(f.type_name, f.field_name, f.request_count) for f in query.fields] == [
        ("Query", "users", 1), ("User", "id", 1),
    ]
    subscription = next(item for item in operations if item.operation_type == "subscription")
    assert subscription.fields == [] and subscription.counted_request_count == 0
    stats = await get_run_stats(postgres_session, run.id)
    assert (stats.total_requests, stats.counted_requests, stats.unique_operations) == (4, 1, 2)
    assert stats.subscription_requests == 1
    assert stats.http_error_count == stats.graphql_error_count == 4
    assert stats.covered_fields == 2
