"""Exercise POST /graphql through the real capture/storage/coverage pipeline."""

import gzip
import hashlib
import json
import uuid

import httpx
import pytest
from starlette.datastructures import State
from sqlalchemy import select, update

from app import proxy
from app.contracts import AnalysisResult, FieldReference
from app.database import Base, get_session
from app.main import app
from app.models import ObservedRequest, RequestFieldUsage, SchemaField, SchemaSnapshot, SchemaType, TestRun as Run
from app.services import create_run, create_service, finish_run, import_sdl
from test_e2e_capture_postgres import postgres_session  # noqa: F401
from test_graphql_proxy import raw_response, upstream  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
async def http_client(postgres_session, monkeypatch):
    async def dependency():
        yield postgres_session

    app.dependency_overrides[get_session] = dependency
    app.dependency_overrides[proxy.get_proxy_session] = dependency
    monkeypatch.setattr(app, "state", State({"coverage_analyzer": None}))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.example",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(proxy.get_proxy_session, None)


async def make_run(session):
    service = await create_service(session, "proxy-e2e-" + uuid.uuid4().hex, "Proxy E2E test")
    snapshot = await import_sdl(
        session, service.id, "type Query { user: User } type User { id: ID! name: String! }",
    )
    other = await import_sdl(
        session, service.id,
        "type Query { user: User other: Boolean } type User { id: ID! name: String! }",
    )
    run = await create_run(session, snapshot.id, "E2E run", {})
    other_run = await create_run(session, other.id, "Other snapshot run", {})
    return run, snapshot, other_run


async def test_http_exchange_committed_before_response_and_correct_snapshot(
    postgres_session, http_client, upstream, monkeypatch, caplog,
):
    session = postgres_session
    run, snapshot, other_run = await make_run(session)
    query = "query GetUser { user { id } } # private-query-text"
    body = json.dumps({"query": query, "operationName": "GetUser", "variables": {"token": "private-variable"}}).encode()
    wire = gzip.compress(b'{"data":{"user":{"id":"private-response"}},"errors":[{"message":"private-error"}]}')
    analyzed = []
    expected_sdl = (await session.get(SchemaSnapshot, snapshot.id)).schema_sdl

    async def analyzer(gql_input, sdl):
        analyzed.append(gql_input)
        assert gql_input.query == query
        assert gql_input.variables == {"token": "private-variable"}
        assert sdl == expected_sdl
        assert "other" not in sdl
        return AnalysisResult(
            status="success", operation_name="GetUser", operation_type="query",
            fields=(FieldReference("Query", "user"), FieldReference("User", "id")),
        )

    monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyzer}))
    requests = upstream(lambda request: raw_response(201, wire, [
        ("Content-Encoding", "gzip"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"),
    ]))
    # Real commit, rather than merely an INSERT/flush, must finish before response.
    commits = []
    real_commit = session.commit

    async def commit():
        await real_commit()
        commits.append(True)

    monkeypatch.setattr(session, "commit", commit)
    async with http_client.stream("POST", "/graphql", content=body, headers={
        "X-Coverage-Run-Id": str(run.id), "Authorization": "Bearer private-bearer",
        "Cookie": "session=private-cookie", "Content-Type": "application/json",
        "X-Forwarded-For": "spoofed",
    }) as response:
        assert commits == [True]
        assert response.status_code == 201
        assert b"".join([chunk async for chunk in response.aiter_raw()]) == wire
        assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert len(analyzed) == len(requests) == 1
    assert requests[0].content == body
    assert requests[0].headers["authorization"] == "Bearer private-bearer"
    assert requests[0].headers["cookie"] == "session=private-cookie"
    assert "x-coverage-run-id" not in requests[0].headers
    assert "x-forwarded-for" not in requests[0].headers

    row = (await session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == run.id))).one()
    assert row.analysis_status == "success"
    assert row.query_hash == hashlib.sha256(query.encode()).hexdigest()
    assert row.http_status == 201 and row.http_method == "POST"
    assert row.duration_ms is not None and row.duration_ms >= 0
    assert row.response_has_data is True and row.graphql_has_errors is True
    field_snapshots = (await session.scalars(
        select(SchemaType.snapshot_id).join(SchemaField, SchemaField.type_id == SchemaType.id)
        .join(RequestFieldUsage, RequestFieldUsage.field_id == SchemaField.id)
        .where(RequestFieldUsage.request_id == row.id)
    )).all()
    assert field_snapshots == [snapshot.id, snapshot.id]

    graph = (await http_client.get(f"/api/runs/{run.id}/coverage")).json()
    assert graph["schemaId"] == str(snapshot.id)
    assert graph["summary"]["coveredFields"] == 2
    fields = {(t["name"], f["name"]): (f["covered"], f["hits"]) for t in graph["types"] for f in t["fields"]}
    assert fields == {("Query", "user"): (True, 1), ("User", "id"): (True, 1), ("User", "name"): (False, 0)}
    other_graph = (await http_client.get(f"/api/runs/{other_run.id}/coverage")).json()
    assert other_graph["summary"]["coveredFields"] == 0
    journal = await http_client.get(f"/api/runs/{run.id}/requests")
    assert journal.json()[0]["analysisStatus"] == "success"

    # Inspect actual persisted column values, not ORM reprs (which omit values).
    persisted = []
    for table in Base.metadata.sorted_tables:
        persisted.extend(dict(row) for row in (await session.execute(select(table))).mappings())
    saved = json.dumps(persisted, default=str) + journal.text + caplog.text
    for secret in (query, "private-query-text", "private-variable", "private-bearer", "private-cookie", "private-response", "private-error"):
        assert secret not in saved


@pytest.mark.parametrize("case,status", [("missing", 404), ("completed", 409), ("failed", 409), ("no_analyzer", 503)])
async def test_preflight_with_real_postgresql(case, status, postgres_session, http_client, upstream, monkeypatch):
    run, _, _ = await make_run(postgres_session)

    async def analyzer(*args):
        pytest.fail("Rejected exchange must not reach analyzer")

    if case != "no_analyzer":
        monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyzer}))
    if case == "completed":
        await finish_run(postgres_session, run.id)
    if case == "failed":
        await postgres_session.execute(update(Run).where(Run.id == run.id).values(status="failed"))
        await postgres_session.commit()
    requests = upstream(lambda request: raw_response())
    response = await http_client.post("/graphql", content=b"{}", headers={
        "X-Coverage-Run-Id": str(uuid.uuid4() if case == "missing" else run.id),
    })
    assert response.status_code == status
    assert requests == []
    assert (await postgres_session.scalars(select(ObservedRequest))).all() == []


@pytest.mark.parametrize("case", ["analyzer_exception", "foreign_field", "finish_during_analysis", "commit_failure", "unsupported"])
async def test_failures_never_invent_coverage(
    case, postgres_session, http_client, upstream, monkeypatch, caplog,
):
    session = postgres_session
    run, _, _ = await make_run(session)
    # Hold an identity-map reference to expose stale run state in the row-lock check.
    cached_run = await session.get(Run, run.id)

    async def analyzer(*args):
        if case == "analyzer_exception":
            raise RuntimeError("private-analyzer-query-token-cookie")
        if case == "finish_during_analysis":
            await session.execute(
                update(Run).where(Run.id == run.id).values(status="completed")
                .execution_options(synchronize_session=False)
            )
            assert cached_run.status == "running"
        return AnalysisResult(
            status="success", operation_type="query",
            fields=(FieldReference("Query", "other" if case == "foreign_field" else "user"),),
        )

    monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyzer}))
    if case == "commit_failure":
        async def fail_commit():
            raise RuntimeError("private-storage-query-token-cookie")
        monkeypatch.setattr(session, "commit", fail_commit)
    requests = upstream(lambda request: raw_response())
    body = b'[{"query":"{ user { id } }"}]' if case == "unsupported" else b'{"query":"{ user { id } }"}'
    response = await http_client.post("/graphql", content=body, headers={"X-Coverage-Run-Id": str(run.id)})
    assert len(requests) == 1
    if case in {"foreign_field", "finish_during_analysis", "commit_failure"}:
        assert response.status_code == 502
        assert "upstream may have executed" in response.text
        # Roll back as a production request-scoped session does on close.
        await session.rollback()
        assert (await session.scalars(select(ObservedRequest))).all() == []
    else:
        assert response.status_code == 200
        row = (await session.scalars(select(ObservedRequest))).one()
        assert row.analysis_status == ("unsupported" if case == "unsupported" else "internal_error")
        assert row.error_code == ("unsupported_request_body" if case == "unsupported" else "analyzer_failure")
    assert (await session.scalars(select(RequestFieldUsage))).all() == []
    if case != "commit_failure":
        graph = (await http_client.get(f"/api/runs/{run.id}/coverage")).json()
        assert graph["summary"]["coveredFields"] == 0
    assert "private-analyzer" not in response.text + caplog.text
    assert "private-storage" not in response.text + caplog.text


@pytest.mark.parametrize("case", ["unknown", "broken_gzip", "decompression_limit", "non_json", "errors_only"])
async def test_unverifiable_response_never_claims_data_or_coverage(
    case, postgres_session, http_client, upstream, monkeypatch,
):
    run, _, _ = await make_run(postgres_session)

    async def analyzer(*args):
        return AnalysisResult(status="success", fields=(FieldReference("Query", "user"),))

    monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyzer}))
    monkeypatch.setattr(proxy, "MAX_ANALYSIS_RESPONSE_BYTES", 128)
    body = b'{"data":{"user":{"id":"1"}}}'
    headers = {}
    if case == "unknown":
        headers["Content-Encoding"] = "unknown"
    elif case == "broken_gzip":
        headers["Content-Encoding"] = "gzip"
        body = gzip.compress(body)[:-1]
    elif case == "decompression_limit":
        headers["Content-Encoding"] = "gzip"
        body = gzip.compress(b'{"data":"' + b"a" * 100_000 + b'"}')
    elif case == "non_json":
        body = b"not-json"
    else:
        body = b'{"errors":[{"message":"failed"}]}'
    upstream(lambda request: raw_response(200, body, headers))
    async with http_client.stream("POST", "/graphql", content=b'{"query":"{ user { id } }"}', headers={
        "X-Coverage-Run-Id": str(run.id),
    }) as response:
        assert response.status_code == 200
        assert b"".join([chunk async for chunk in response.aiter_raw()]) == body
    row = (await postgres_session.scalars(select(ObservedRequest))).one()
    assert row.analysis_status == "success"
    assert row.response_has_data is (False if case == "errors_only" else None)
    assert row.graphql_has_errors is (True if case == "errors_only" else None)
    graph = (await http_client.get(f"/api/runs/{run.id}/coverage")).json()
    assert graph["summary"]["coveredFields"] == 0
