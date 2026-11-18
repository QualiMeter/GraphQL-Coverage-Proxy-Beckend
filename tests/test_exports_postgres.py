import csv
import io
import json
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import insert, select
from starlette.datastructures import State

from app import export_api, export_data, export_files
from app.analyzer_adapter import analyze_e2e_query
from app.auth import hash_password
from app.auth_models import ServiceGrant, User
from app.database import get_session_factory
from app.main import app
from app.models import ObservedRequest, TestExecution as Execution
from app.services import create_run, create_service, import_sdl
from test_browser_auth_postgres import ORIGIN, PASSWORD, login
from test_graphql_proxy import raw_response, upstream
from test_multi_service_postgres import isolated_database, routed_client
from test_test_coverage_postgres import start, exchange, finish

pytestmark = pytest.mark.integration
SDL = "type Query { user: User health: String } type User { id: ID! name: String email: String }"


@pytest.fixture
async def exports_context(isolated_database, routed_client, monkeypatch, upstream):
    monkeypatch.setenv("COVERAGE_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setattr(app, "state", State({"coverage_analyzer": analyze_e2e_query}))
    async with isolated_database() as session:
        service = await create_service(session, "exports", "=Магазин", is_public=True,
                                       endpoint_url="https://93.184.216.34/graphql")
        snapshot = await import_sdl(session, service.id, SDL)
        run = await create_run(session, snapshot.id, "Прогон QA", {"secret": "do-not-export-metadata"})
    upstream(lambda request: raw_response(body=b'{"data":{"user":{"id":"1","name":"private-response-value"}}}'))
    client, run_id = routed_client, str(run.id)
    first, _ = await start(client, run_id, test_id="=SUM(1,2)", name="Проверка корзины")
    second, _ = await start(client, run_id, test_id="cart::failed")
    assert (await exchange(client, run_id, first["id"])).status_code == 200
    assert (await exchange(client, run_id, second["id"], "{ user { id } }")).status_code == 200
    assert (await finish(client, run_id, first["id"])).status_code == 200
    assert (await finish(client, run_id, second["id"], "failed")).status_code == 200
    return client, isolated_database, service, snapshot, run, first, second


@pytest.mark.parametrize("dataset", ["coverage", "operations", "requests", "tests", "test-matrix"])
@pytest.mark.parametrize("format", ["json", "csv"])
async def test_download_has_metadata_and_no_private_payloads(exports_context, dataset, format):
    client, _, service, snapshot, run, _, _ = exports_context
    response = await client.get(f"/api/runs/{run.id}/exports/{dataset}", params={"format": format})
    assert response.status_code == 200, response.text
    assert f'{dataset}-{run.id}.{format}' in response.headers["Content-Disposition"]
    assert response.headers["X-Export-Format-Version"] == "1"
    assert response.headers["Cache-Control"] == "no-store"
    assert int(response.headers["Content-Length"]) == len(response.content)
    assert all(secret not in response.text for secret in (
        "do-not-export-metadata", "private-response-value", "safeGraphqlDocument", "graphqlErrorDetails", "endpointUrl",
    ))
    if format == "json":
        document = response.json()
        export_api.ExportDocument.model_validate(document)
        assert document["formatVersion"] == 1 and document["dataset"] == dataset
        assert document["rowCount"] == len(document["rows"]) == int(response.headers["X-Total-Count"])
        assert document["metadata"]["service"]["id"] == str(service.id)
        assert document["metadata"]["snapshot"]["id"] == str(snapshot.id)
        assert document["metadata"]["runs"][0]["id"] == str(run.id)
    else:
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert rows[0]["recordType"] == "metadata" and rows[0]["serviceName"] == "\t=Магазин"
        assert rows[0]["snapshotId"] == str(snapshot.id) and rows[0]["formatVersion"] == "1"
        assert all(row["recordType"] == "data" for row in rows[1:])
        assert len(rows) - 1 == int(rows[0]["rowCount"])
        if dataset in {"tests", "test-matrix"}:
            assert any(row["testId"] == "\t=SUM(1,2)" for row in rows[1:])


async def test_export_matches_coverage_and_filtered_source_tables(exports_context):
    client, _, _, _, run, first, _ = exports_context
    base = f"/api/runs/{run.id}"
    graph = (await client.get(base + "/coverage")).json()
    coverage = (await client.get(base + "/exports/coverage")).json()
    expected = {(t["name"], f["name"]): f["hits"] for t in graph["types"] for f in t["fields"]}
    assert {(r["typeName"], r["fieldName"]): r["hits"] for r in coverage["rows"]} == expected
    assert coverage["summary"] == graph["summary"]
    filtered = await client.get(base + "/exports/coverage", params={"covered": False})
    assert {r["fieldName"] for r in filtered.json()["rows"]} == {"health", "email"}
    for dataset, query in (
        ("requests", {"testExecutionId": first["id"], "typeName": "User", "fieldName": "name", "sortBy": "durationMs"}),
        ("tests", {"status": "passed", "typeName": "User", "fieldName": "name", "fieldMode": "selected"}),
        ("operations", {"operationType": "query"}),
    ):
        source = await client.get(base + "/" + dataset, params=query)
        exported = await client.get(base + "/exports/" + dataset, params={**query, "format": "json"})
        assert exported.status_code == 200, exported.text
        assert exported.json()["rows"] == source.json()
    matrix = await client.get(base + "/exports/test-matrix", params={"status": "passed", "typeName": "User"})
    assert matrix.status_code == 200, matrix.text
    source_fields = (await client.get(base + f"/tests/{first['id']}/fields")).json()
    assert [{key: row[key] for key in source_fields[0]} for row in matrix.json()["rows"]] == [
        row for row in source_fields if row["typeName"] == "User"]


@pytest.mark.parametrize("dataset,query", [
    ("coverage", {"typeName": "Missing"}), ("operations", {"operationName": "Missing"}),
    ("requests", {"operationName": "Missing"}), ("tests", {"testId": "Missing"}),
    ("test-matrix", {"testId": "Missing"}),
])
async def test_no_matches_produce_valid_empty_files(exports_context, dataset, query):
    client, _, _, _, run, _, _ = exports_context
    for format in ("json", "csv"):
        response = await client.get(f"/api/runs/{run.id}/exports/{dataset}", params={**query, "format": format})
        assert response.status_code == 200, response.text
        assert response.headers["X-Total-Count"] == "0"
        if format == "json":
            assert response.json()["rows"] == [] and response.json()["rowCount"] == 0
        else:
            rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
            assert len(rows) == 1 and rows[0]["recordType"] == "metadata" and rows[0]["rowCount"] == "0"


async def test_all_matching_requests_and_tests_are_exported_beyond_a_table_page(exports_context):
    client, sessions, _, _, run, _, _ = exports_context
    async with sessions.begin() as session:
        await session.execute(insert(ObservedRequest), [{
            "id": uuid.uuid4(), "run_id": run.id, "http_method": "POST", "http_status": 200,
            "analysis_status": "unsupported", "response_has_data": False, "graphql_has_errors": False,
        } for _ in range(1203)])
        await session.execute(insert(Execution), [{
            "id": uuid.uuid4(), "run_id": run.id, "test_id": f"bulk::{i}", "test_key": f"{i:064x}",
            "name": f"Тест {i}", "framework": "pytest", "attempt": 1,
        } for i in range(503)])
    for dataset, expected in (("requests", 1205), ("tests", 505)):
        response = await client.get(f"/api/runs/{run.id}/exports/{dataset}", params={"limit": 1, "offset": 1})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["rowCount"] == len(body["rows"]) == expected
        assert "limit" not in body["filters"] and "offset" not in body["filters"]
        assert len({row["id"] for row in body["rows"]}) == expected


async def test_exports_enforce_roles_and_service_grants(exports_context):
    _, sessions, service, _, run, _, _ = exports_context
    async with sessions() as session:
        viewer = User(username="export-viewer", role="viewer", password_hash=hash_password(PASSWORD))
        analyst = User(username="export-analyst", role="analyst", password_hash=hash_password(PASSWORD))
        session.add_all([viewer, analyst])
        await session.commit()
        viewer_id, analyst_id = viewer.id, analyst.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        for dataset in ("coverage", "operations"):
            assert (await client.get(f"/api/runs/{run.id}/exports/{dataset}")).status_code == 200
        for dataset in ("requests", "tests", "test-matrix"):
            assert (await client.get(f"/api/runs/{run.id}/exports/{dataset}")).status_code == 401
        await login(client, "export-viewer")
        async with sessions.begin() as session:
            session.add(ServiceGrant(user_id=viewer_id, service_id=service.id))
        assert (await client.get(f"/api/runs/{run.id}/exports/tests")).status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        await login(client, "export-analyst")
        for dataset in ("requests", "tests", "test-matrix"):
            assert (await client.get(f"/api/runs/{run.id}/exports/{dataset}")).status_code == 404
        async with sessions.begin() as session:
            session.add(ServiceGrant(user_id=analyst_id, service_id=service.id))
        for dataset in ("requests", "tests", "test-matrix"):
            response = await client.get(f"/api/runs/{run.id}/exports/{dataset}")
            assert response.status_code == 200, response.text


async def test_export_limits_and_invalid_filters_return_errors_without_partial_attachments(exports_context, monkeypatch):
    client, _, _, _, run, _, _ = exports_context
    url = f"/api/runs/{run.id}/exports/coverage"
    for query in ({"format": "xlsx"}, {"fieldName": "name"}, {"unknownFilter": "value"}):
        response = await client.get(url, params=query)
        assert response.status_code == 422, response.text
    monkeypatch.setattr(export_files, "MAX_EXPORT_ROWS", 1)
    response = await client.get(url)
    assert response.status_code == 413 and "Content-Disposition" not in response.headers
    monkeypatch.setattr(export_files, "MAX_EXPORT_ROWS", 100_000)
    monkeypatch.setattr(export_files, "MAX_EXPORT_BYTES", 100)
    response = await client.get(url)
    assert response.status_code == 413 and "Content-Disposition" not in response.headers


async def test_comparison_exports_every_filtered_field_and_preserves_api_validation(exports_context):
    client, sessions, _, snapshot, first, _, _ = exports_context
    assert (await client.post(f"/api/runs/{first.id}/finish")).status_code == 200
    async with sessions() as session:
        second = await create_run(session, snapshot.id, "Second", {})
    assert (await exchange(client, str(second.id), query="{ health }")).status_code == 200
    body = {"runIds": [str(first.id), str(second.id)], "baselineRunId": str(first.id),
            "fields": {"filter": "changed", "limit": 1, "offset": 1}, "types": {"limit": 1, "offset": 1}}
    url = "/api/runs/compare/export"
    assert (await client.post(url, json=body)).status_code == 409
    assert (await client.post(f"/api/runs/{second.id}/finish")).status_code == 200
    source = await client.post("/api/runs/compare", json={**body, "fields": {"filter": "changed"}})
    for format in ("json", "csv"):
        response = await client.post(url, params={"format": format}, json=body)
        assert response.status_code == 200, response.text
        if format == "json":
            result = response.json()
            assert result["rows"] == source.json()["fields"]["items"]
            assert result["rowCount"] == 4
            assert result["summary"]["comparison"] == source.json()["comparison"]
            assert len(result["summary"]["types"]["items"]) == 2
        else:
            rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
            assert len(rows) == 5 and rows[1]["hitsByRun"]
    duplicate = await client.post(url, json={**body, "runIds": [str(first.id), str(first.id)]})
    assert duplicate.status_code == 422
    oversized = await client.post(url, content=b"x" * 16_385)
    assert oversized.status_code == 413


async def test_export_uses_one_snapshot_during_concurrent_capture(exports_context, monkeypatch):
    client, _, _, _, run, _, _ = exports_context
    original = export_files.ExportFile.start

    async def while_starting(writer, *args):
        await original(writer, *args)
        response = await exchange(client, str(run.id), query="{ health }")
        assert response.status_code == 200

    monkeypatch.setattr(export_files.ExportFile, "start", while_starting)
    response = await client.get(f"/api/runs/{run.id}/exports/coverage")
    assert response.status_code == 200, response.text
    exported = response.json()
    assert exported["summary"]["coveredFields"] == 3
    assert next(row for row in exported["rows"] if row["fieldName"] == "health")["hits"] == 0
    graph = (await client.get(f"/api/runs/{run.id}/coverage")).json()
    assert graph["summary"]["coveredFields"] == 4


async def test_database_session_is_released_before_downloading(exports_context):
    original_client, sessions, _, _, run, _, _ = exports_context
    active = set()

    @asynccontextmanager
    async def tracked_sessions():
        async with sessions() as session:
            active.add(session)
            try:
                yield session
            finally:
                active.remove(session)

    async def checked_app(scope, receive, send):
        async def checked_send(message):
            if message["type"] in {"http.response.start", "http.response.body"}:
                assert not active
            await send(message)
        await app(scope, receive, checked_send)

    previous = app.dependency_overrides[get_session_factory]
    app.dependency_overrides[get_session_factory] = lambda: tracked_sessions
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=checked_app), base_url=ORIGIN,
                                     headers=dict(original_client.headers)) as client:
            response = await client.get(f"/api/runs/{run.id}/exports/requests")
            assert response.status_code == 200 and response.json()["rowCount"] == 2
    finally:
        app.dependency_overrides[get_session_factory] = previous


async def test_processing_limit_closes_temp_file_before_selecting_export_rows(exports_context, monkeypatch):
    client, _, _, _, run, _, _ = exports_context
    writer = export_files.ExportFile("json")
    monkeypatch.setattr(export_api, "ExportFile", lambda format: writer)
    monkeypatch.setattr(export_data, "MAX_EXPORT_BYTES", 10)
    response = await client.get(f"/api/runs/{run.id}/exports/requests")
    assert response.status_code == 413 and writer.file.closed
    assert "Content-Disposition" not in response.headers


async def test_temporary_file_error_returns_a_safe_error_and_closes_file(exports_context, monkeypatch):
    client, _, _, _, run, _, _ = exports_context
    writer = export_files.ExportFile("json")

    def disk_error(value):
        raise OSError("secret-filesystem-details")

    monkeypatch.setattr(writer, "write", disk_error)
    monkeypatch.setattr(export_api, "ExportFile", lambda format: writer)
    response = await client.get(f"/api/runs/{run.id}/exports/coverage")
    assert response.status_code == 503 and writer.file.closed
    assert "secret-filesystem-details" not in response.text and "Content-Disposition" not in response.headers


async def test_private_service_exports_are_not_visible_without_a_grant(exports_context):
    _, sessions, _, _, _, _, _ = exports_context
    async with sessions() as session:
        service = await create_service(session, "private-export", "Private")
        snapshot = await import_sdl(session, service.id, "type Query { secret: String }")
        run = await create_run(session, snapshot.id, "Hidden", {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        for dataset in ("coverage", "operations"):
            response = await client.get(f"/api/runs/{run.id}/exports/{dataset}")
            assert response.status_code == 404 and "Hidden" not in response.text
            missing = await client.get(f"/api/runs/{uuid.uuid4()}/exports/{dataset}")
            assert missing.status_code == 404 and response.json() == missing.json()


async def test_comparison_export_requires_csrf_for_browser_sessions(exports_context):
    admin, sessions, _, snapshot, first, _, _ = exports_context
    assert (await admin.post(f"/api/runs/{first.id}/finish")).status_code == 200
    async with sessions() as session:
        second = await create_run(session, snapshot.id, "Second", {})
        session.add(User(username="compare-viewer", role="viewer", password_hash=hash_password(PASSWORD)))
        await session.commit()
    assert (await admin.post(f"/api/runs/{second.id}/finish")).status_code == 200
    body = {"runIds": [str(first.id), str(second.id)], "baselineRunId": str(first.id)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        headers, _ = await login(client, "compare-viewer")
        assert (await client.post("/api/runs/compare/export", json=body)).status_code == 403
        response = await client.post("/api/runs/compare/export", json=body, headers=headers)
        assert response.status_code == 200, response.text


async def test_coverage_export_supports_current_table_filters_and_abstract_fields(exports_context):
    client, sessions, service, _, _, _, _ = exports_context
    async with sessions() as session:
        snapshot = await import_sdl(session, service.id, """
            scalar Money
            enum State { ACTIVE }
            interface Node { id: ID! }
            type User implements Node { id: ID! price: Money legacy: String @deprecated }
            type Query { user: User users: [User!]! node: Node state: State }
        """)
        run = await create_run(session, snapshot.id, "Advanced filters", {})
    url = f"/api/runs/{run.id}/exports/coverage"
    for params, expected in (
        ({"returns": "list"}, {"Query.users"}),
        ({"returns": "object"}, {"Query.user"}),
        ({"returns": "scalar"}, {"User.id", "User.price", "User.legacy", "Node.id"}),
        ({"deprecated": True}, {"User.legacy"}),
        ({"search": " user.price "}, {"User.price"}),
        ({"search": "%"}, set()),
        ({"minHits": 1}, set()),
    ):
        response = await client.get(url, params=params)
        assert response.status_code == 200, response.text
        assert {row["typeName"] + "." + row["fieldName"] for row in response.json()["rows"]} == expected
    response = await client.get(url)
    assert response.json()["summary"]["totalFields"] == 7
    assert next(row for row in response.json()["rows"] if row["typeName"] == "Node")["countedInDenominator"] is False
    assert (await client.get(url, params={"minHits": 2, "maxHits": 1})).status_code == 422


async def test_operation_table_filters_and_grouped_request_errors(exports_context, upstream):
    client, _, _, _, run, _, _ = exports_context
    upstream(lambda request: raw_response(status=503, body=b'{"data":{"user":{"email":null}},"errors":[{"message":"secret-error-payload"}]}'))
    assert (await exchange(client, str(run.id), query="query Broken { user { email } }")).status_code == 503
    upstream(lambda request: raw_response(body=b'{"data":{"user":{"id":"1"}}}'))
    assert (await exchange(client, str(run.id), query="{ missing }")).status_code == 200
    base = f"/api/runs/{run.id}"
    for query in (
        {"search": " BROKEN "}, {"hasErrors": True},
        {"typeName": "User", "fieldName": "email"}, {"sortBy": "errors", "hasErrors": True},
    ):
        response = await client.get(base + "/exports/operations", params=query)
        assert response.status_code == 200, response.text
        assert [row["operationName"] for row in response.json()["rows"]] == ["Broken"]
        assert "secret-error-payload" not in response.text
    for group, query in (("http", {"httpStatusClass": 5}), ("analyzer", {"analysisStatus": "validation_error"})):
        source = await client.get(base + "/requests", params=query)
        exported = await client.get(base + "/exports/requests", params={"errorGroup": group})
        assert exported.status_code == 200, exported.text
        assert exported.json()["rows"] == source.json() and exported.json()["rowCount"] == 1


def test_committed_json_schema_matches_openapi_model():
    from pathlib import Path
    schema = json.loads((Path(__file__).resolve().parents[1] / "docs/export-format-v1.schema.json").read_text(encoding="utf-8"))
    assert schema.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema == export_api.ExportDocument.model_json_schema(by_alias=True)


def test_openapi_exposes_downloads_filters_and_protected_exports():
    schema = app.openapi()
    for dataset in ("coverage", "operations", "requests", "tests", "test-matrix"):
        route = schema["paths"]["/api/runs/{run_id}/exports/" + dataset]["get"]
        assert {"application/json", "text/csv"} <= route["responses"]["200"]["content"].keys()
        assert "format" in {parameter["name"] for parameter in route["parameters"]}
        if dataset in ("requests", "tests", "test-matrix"):
            assert route["security"]
        else:
            assert {} in route["security"]
    comparison = schema["paths"]["/api/runs/compare/export"]["post"]
    assert "X-CSRF-Token" in {param["name"] for param in comparison["parameters"]}
