import hashlib
import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.contracts import AnalysisResult, FieldReference, RequestObservationInput
from app.analyzer_adapter import analyze_e2e_query
from app.database import get_session, get_session_factory
from app.main import app
from app.e2e_capture import capture_e2e_exchange
from app.models import (
    GraphQLService, ObservedRequest, SchemaField, SchemaSnapshot, SchemaType,
    TestRun as RunModel,
)
from app.api_models import RunComparisonRequest
from app.auth import Principal
from app.run_comparison import RunComparisonError, compare_run_series
from app.run_analytics import get_run_stats, list_operations, operation_key
from app.services import (
    NotFoundError, create_run, create_service, finish_run, get_run_graph, import_sdl,
    record_observation,
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
        "analysis_status_counts": {"success": 0, "parse_error": 0, "validation_error": 0,
                                   "unsupported": 0, "internal_error": 0}, "p95_duration_ms": None,
        "total_requests": 0, "counted_requests": 0, "unique_operations": 0,
        "query_requests": 0, "mutation_requests": 0, "subscription_requests": 0,
        "http_error_count": 0, "graphql_error_count": 0, "avg_duration_ms": None,
        "total_fields": 7, "covered_fields": 0, "coverage_percent": 0.0, "request_timeline": [],
    }
    assert await list_operations(session, run.id) == []
    for _ in range(2):
        await observe(session, run.id, name=name, fields=(("Query", "health"), ("Query", "health")))
    stats = await get_run_stats(session, run.id)
    assert stats.avg_duration_ms is None
    assert stats.request_timeline[0].avg_duration_ms is None
    operation, = await list_operations(session, run.id)
    assert operation.avg_duration_ms is None
    assert operation.operation_name == name
    assert operation.request_count == operation.fields[0].hits == 2
    assert stats.coverage_percent == (await get_run_graph(session, run.id)).summary.coverage_percent


async def test_three_run_comparison_matches_coverage_engine_and_classifies_field_history(postgres_session):
    service = await create_service(postgres_session, "comparison-" + uuid.uuid4().hex,
                                   "Comparison", is_public=True)
    schema = await import_sdl(postgres_session, service.id,
        "type Query { user: User health: String } type User { id: ID email: String } "
        "type Mutation { createUser: User deleteUser: Boolean }")
    series = []
    for run_index, (name, field_hits) in enumerate((
        ("First", {("Query", "user"): 2, ("User", "id"): 2, ("Mutation", "deleteUser"): 1}),
        ("Second", {("Query", "user"): 3, ("User", "id"): 2, ("User", "email"): 1,
                    ("Mutation", "createUser"): 2}),
        ("Third", {("Query", "user"): 3, ("User", "id"): 2, ("Mutation", "createUser"): 2}),
    )):
        run = await create_run(postgres_session, schema.id, name, {})
        for (type_name, field_name), count in field_hits.items():
            for _ in range(count):
                await observe(postgres_session, run.id, query_hash=(field_name + name).encode().hex()[:64].ljust(64, "0"),
                              name=f"{name}_{field_name}", fields=((type_name, field_name),))
        await finish_run(postgres_session, run.id)
        await postgres_session.execute(
            text("UPDATE test_runs SET started_at = :started WHERE id = :run_id"),
            {"started": datetime(2026, 11, run_index + 1, tzinfo=timezone.utc), "run_id": run.id},
        )
        await postgres_session.commit()
        series.append(run)

    payload = RunComparisonRequest.model_validate({
        "runIds": [str(run.id) for run in series], "baselineRunId": str(series[0].id),
        "targetRunId": str(series[2].id),
    })
    comparison = await compare_run_series(postgres_session, payload, Principal(role="admin"))
    assert comparison.snapshot_id == schema.id
    assert [run.name for run in comparison.runs] == ["First", "Second", "Third"]
    assert [(run.total_fields, run.covered_fields, run.coverage_percent) for run in comparison.runs] == [
        (6, 3, 50.0), (6, 4, 66.67), (6, 3, 50.0),
    ]
    assert comparison.comparison.percentage_point_delta == 0
    assert comparison.field_categories == {
        "newlyCovered": 1, "noLongerCovered": 1, "stillCovered": 2, "stillUncovered": 2,
        "alwaysCovered": 2, "neverCovered": 1, "intermittentlyCovered": 3,
    }
    assert [delta["deltaPp"] for delta in comparison.comparison.adjacent_coverage_deltas_pp] == [16.67, -16.67]
    email = next(item for item in comparison.fields.items if (item.type_name, item.field_name) == ("User", "email"))
    assert [item.hits for item in email.hits_by_run] == [0, 1, 0]
    assert email.coverage_frequency_percent == 33.33 and email.coverage_transitions == 2
    assert email.baseline_to_target == "stillUncovered" and email.stability == "intermittentlyCovered"
    assert comparison.types.total == 3
    assert {item.type_name: item.root_role for item in comparison.types.items} == {
        "Mutation": "mutation", "Query": "query", "User": None,
    }
    for run, compared_run in zip(series, comparison.runs):
        stats = await get_run_stats(postgres_session, run.id)
        graph = await get_run_graph(postgres_session, run.id)
        assert compared_run.covered_fields == stats.covered_fields == graph.summary.covered_fields
        assert compared_run.total_requests == stats.total_requests
        assert compared_run.counted_requests == stats.counted_requests

    default_target = await compare_run_series(
        postgres_session,
        RunComparisonRequest.model_validate({
            "runIds": [str(run.id) for run in series], "baselineRunId": str(series[0].id),
            "fields": {"filter": "changed", "limit": 1},
        }),
        Principal(role="admin"),
    )
    assert default_target.target_run_id == series[2].id
    assert default_target.fields.total == 2 and len(default_target.fields.items) == 1
    assert default_target.field_categories == comparison.field_categories


async def test_run_comparison_rejects_active_runs_and_different_snapshots(postgres_session, analytics_run):
    _, schema, finished = analytics_run
    await finish_run(postgres_session, finished.id)
    active = await create_run(postgres_session, schema.id, "Active", {})
    active_payload = RunComparisonRequest.model_validate({
        "runIds": [str(finished.id), str(active.id)], "baselineRunId": str(finished.id),
    })
    with pytest.raises(RunComparisonError) as error:
        await compare_run_series(postgres_session, active_payload, Principal(role="admin"))
    assert error.value.code == "RUN_NOT_COMPLETED"
    await finish_run(postgres_session, active.id)

    other_schema = await import_sdl(
        postgres_session,
        (await create_service(
            postgres_session, "other-schema-" + uuid.uuid4().hex, "Other schema"
        )).id,
        "type Query { different: Boolean }",
    )
    other_run = await create_run(postgres_session, other_schema.id, "Other", {})
    await finish_run(postgres_session, other_run.id)
    mismatch_payload = RunComparisonRequest.model_validate({
        "runIds": [str(finished.id), str(other_run.id)], "baselineRunId": str(finished.id),
    })
    with pytest.raises(RunComparisonError) as error:
        await compare_run_series(postgres_session, mismatch_payload, Principal(role="admin"))
    assert error.value.code == "SNAPSHOT_MISMATCH"


async def test_run_comparison_http_acl_and_response_omit_run_metadata():
    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip("TEST_DATABASE_URL missing; PostgreSQL integration NOT verified")
    configured_url = os.environ.get("DATABASE_URL", "postgresql+asyncpg://coverage:coverage_dev@localhost:5432/coverage")
    if make_url(test_url).database == make_url(configured_url).database:
        pytest.fail("TEST_DATABASE_URL must name a database separate from DATABASE_URL")
    engine = create_async_engine(test_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    service_ids = []
    snapshot_ids = []
    run_ids = []
    app.dependency_overrides[get_session_factory] = lambda: sessions
    try:
        async with sessions() as setup:
            service = await create_service(setup, "comparison-http-" + uuid.uuid4().hex,
                                          "Public comparison", is_public=True)
            schema = await import_sdl(setup, service.id, "type Query { visible: Boolean }")
            service_ids.append(service.id)
            snapshot_ids.append(schema.id)
            runs = []
            for run_index, name in enumerate(("Before", "After")):
                run = await create_run(setup, schema.id, name, {"private": "must not leak"})
                run.started_at = datetime(2026, 11, 10 + run_index, tzinfo=timezone.utc)
                await setup.commit()
                await finish_run(setup, run.id)
                runs.append(run)
                run_ids.append(run.id)

            private = await create_service(setup, "comparison-private-" + uuid.uuid4().hex,
                                           "Private comparison")
            private_schema = await import_sdl(setup, private.id,
                                              "type Query { hidden: Boolean }")
            service_ids.append(private.id)
            snapshot_ids.append(private_schema.id)
            hidden = []
            for name in ("Hidden one", "Hidden two"):
                run = await create_run(setup, private_schema.id, name, {})
                await finish_run(setup, run.id)
                hidden.append(run)
                run_ids.append(run.id)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            body = {"runIds": [str(run.id) for run in runs], "baselineRunId": str(runs[0].id)}
            response = await client.post("/api/runs/compare", json=body)
            assert response.status_code == 200
            data = response.json()
            assert data["targetRunId"] == str(runs[1].id)
            assert [run["name"] for run in data["runs"]] == ["Before", "After"]
            assert all("metadata" not in run for run in data["runs"])

            inaccessible = await client.post("/api/runs/compare", json={
                "runIds": [str(runs[0].id), str(hidden[0].id)],
                "baselineRunId": str(runs[0].id),
            })
            assert inaccessible.status_code == 404
    finally:
        app.dependency_overrides.pop(get_session_factory, None)
        async with sessions() as cleanup:
            async with cleanup.begin():
                if run_ids:
                    await cleanup.execute(delete(RunModel).where(RunModel.id.in_(run_ids)))
                if snapshot_ids:
                    type_ids = select(SchemaType.id).where(SchemaType.snapshot_id.in_(snapshot_ids))
                    await cleanup.execute(delete(SchemaField).where(SchemaField.type_id.in_(type_ids)))
                    await cleanup.execute(delete(SchemaType).where(SchemaType.snapshot_id.in_(snapshot_ids)))
                    await cleanup.execute(delete(SchemaSnapshot).where(SchemaSnapshot.id.in_(snapshot_ids)))
                if service_ids:
                    await cleanup.execute(delete(GraphQLService).where(GraphQLService.id.in_(service_ids)))
        await engine.dispose()


async def test_aggregates_group_identity_errors_fields_timeline_and_history(postgres_session, analytics_run):
    _, schema, run = analytics_run
    session = postgres_session
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
    await observe(session, run.id, query_hash=None, fields=(("Query", "health"),))
    await observe(session, run.id, status="validation_error", data=False, errors=True)

    operations = await list_operations(session, run.id)
    assert len(operations) == 6
    a = next(item for item in operations if item.operation_key == operation_key("a" * 64, "GetUsers", "query"))
    assert (a.request_count, a.counted_request_count, a.http_error_count, a.graphql_error_count) == (4, 2, 2, 2)
    assert a.avg_duration_ms == 20.0
    assert a.last_seen_at == datetime(2026, 11, 14, 0, 2, tzinfo=timezone.utc)
    assert [(f.type_name, f.field_name, f.hits) for f in a.fields] == [
        ("Query", "users", 2), ("User", "id", 2), ("User", "name", 2),
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
    sessions = async_sessionmaker(bind=postgres_session.bind, expire_on_commit=False,
                                 join_transaction_mode="create_savepoint")
    app.dependency_overrides[get_session_factory] = lambda: sessions
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
            assert operations[0]["fields"] == [{"typeName": "Query", "fieldName": "health", "hits": 1}]
            filtered = await client.get(f"/api/runs/{run.id}/operations?operationType=mutation")
            assert filtered.status_code == 200 and filtered.json() == []
            assert (await client.get(f"/api/runs/{run.id}/operations?operationType=bogus")).status_code == 422
            assert (await client.get(f"/api/runs/{run.id}/requests")).status_code == 401
            assert (await client.get(f"/api/runs/{run.id}/requests", headers=headers)).status_code == 200
            assert (await client.post("/graphql", json={})).status_code == 401
            monkeypatch.delenv("COVERAGE_API_KEY")
            for endpoint in ("operations", "stats"):
                assert (await client.get(f"/api/runs/{run.id}/{endpoint}")).status_code == 200
            monkeypatch.setenv("COVERAGE_API_KEY", TEST_KEY)
            await client.patch(f"/api/services/{service.id}", headers=headers, json={"isPublic": False})
            for endpoint in ("operations", "stats"):
                assert (await client.get(f"/api/runs/{run.id}/{endpoint}")).status_code == 404
    finally:
        app.dependency_overrides.pop(get_session_factory, None)
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
    assert [(f.type_name, f.field_name, f.hits) for f in query.fields] == [
        ("Query", "users", 1), ("User", "id", 1),
    ]
    subscription = next(item for item in operations if item.operation_type == "subscription")
    assert subscription.fields == [] and subscription.counted_request_count == 0
    stats = await get_run_stats(postgres_session, run.id)
    assert (stats.total_requests, stats.counted_requests, stats.unique_operations) == (4, 1, 2)
    assert stats.subscription_requests == 1
    assert stats.http_error_count == stats.graphql_error_count == 4
    assert stats.covered_fields == 2


@pytest.mark.parametrize("uncounted_data", [False, None])
async def test_operation_field_hits_match_coverage_engine(postgres_session, analytics_run, uncounted_data):
    _, _, run = analytics_run
    session = postgres_session
    fields = (("Query", "users"), ("User", "id"), ("User", "id"))
    await observe(session, run.id, fields=fields)
    await observe(session, run.id, fields=fields, http=500, errors=True)
    await observe(session, run.id, fields=fields + (("User", "name"),), data=uncounted_data)
    operation, = await list_operations(session, run.id)
    assert operation.request_count == 3
    assert operation.counted_request_count == 2
    assert [(field.type_name, field.field_name, field.hits) for field in operation.fields] == [
        ("Query", "users", 2), ("User", "id", 2),
    ]
    graph = await get_run_graph(session, run.id)
    engine_hits = {(node.name, field.name): field.hits for node in graph.types for field in node.fields}
    assert all(field.hits == engine_hits[(field.type_name, field.field_name)] for field in operation.fields)
    assert engine_hits[("User", "name")] == 0
    await observe(session, run.id, query_hash="b" * 64, fields=fields, data=uncounted_data)
    uncounted = next(item for item in await list_operations(session, run.id)
                     if item.operation_key == operation_key("b" * 64, "GetUsers", "query"))
    assert uncounted.request_count == 1 and uncounted.counted_request_count == 0
    assert uncounted.fields == []
