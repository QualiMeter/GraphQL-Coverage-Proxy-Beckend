"""Real capture → PostgreSQL → API, coverage invariants and evidence privacy."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, DBAPIError
from starlette.datastructures import State

from app.analyzer_adapter import analyze_e2e_query
from app.contracts import AnalysisResult, FieldEvidence, FieldReference, RequestObservationInput
from app.e2e_capture import capture_e2e_exchange
from app.evidence_analytics import EvidenceFilters, get_field_evidence
from app.main import app
from app.models import ObservedRequest, RequestFieldEvidence, RequestFieldUsage, SchemaField, SchemaType
from app.services import (ConflictError, InvalidReferenceError, create_run, create_service, finish_run,
                          get_run_graph, import_sdl, record_observation)
from test_e2e_capture_postgres import postgres_session  # noqa: F401
from test_proxy_e2e_postgres import http_client, make_run  # noqa: F401
from test_graphql_proxy import upstream, raw_response  # noqa: F401
from test_browser_auth_postgres import auth_context, grant, login, TEST_KEY  # noqa: F401
from test_multi_service_postgres import isolated_database  # noqa: F401
from test_field_evidence import SDL

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
QUERY = '{ user { id name } }'


async def capture(session, run, payload, query=QUERY):
    body = payload if isinstance(payload, (bytes, type(None))) else json.dumps(payload).encode()
    return await capture_e2e_exchange(session, run_id=run.id, request_body=json.dumps({'query': query}).encode(),
        decoded_response_body=body, http_status=200, duration_ms=2, analyzer=analyze_e2e_query)


async def test_aggregate_all_states_legacy_and_unchanged_schema_coverage(postgres_session, http_client):
    run, _, other = await make_run(postgres_session)
    payloads = [{'data': {'user': {'id': None, 'name': 'private-user-value'}}},
                {'data': {'user': {'id': 'private-id'}}},
                {'data': {'user': None}},
                {'data': {'user': {'id': None}}, 'errors': [{}]},
                {'errors': [{}]}, {'data': None}, b'bad']
    ids = [await capture(postgres_session, run, payload) for payload in payloads]
    legacy = await record_observation(postgres_session, RequestObservationInput(
        run_id=run.id, analysis=AnalysisResult(status='success', fields=(FieldReference('User', 'name'),)),
        http_method='POST', http_status=200, response_has_data=True, graphql_has_errors=False))
    # A genuinely historic row has no evidence row; it must not become absence.
    assert await postgres_session.scalar(select(func.count()).select_from(RequestFieldEvidence).where(RequestFieldEvidence.request_id == legacy)) == 0
    page = await get_field_evidence(postgres_session, run.id, EvidenceFilters())
    assert page.request_count == 8 and page.evidence_unavailable_request_count == 3
    name = next(f for f in page.fields if f.field_name == 'name')
    assert name.model_dump(exclude={'field_id', 'type_name', 'field_name'}) == {
        'selected_request_count': 8, 'selected_hits': 5, 'observed_hits': 1,
        'not_observed_count': 1, 'indeterminate_count': 3, 'unavailable_count': 3}
    for f in page.fields:
        assert f.selected_request_count == f.observed_hits + f.not_observed_count + f.indeterminate_count + f.unavailable_count
    graph = await get_run_graph(postgres_session, run.id)
    assert graph.summary.coverage_percent == 100.0
    assert {(t.name, f.name): f.hits for t in graph.types for f in t.fields} == {
        ('Query', 'user'): 4, ('User', 'id'): 4, ('User', 'name'): 5}
    assert (await get_run_graph(postgres_session, other.id)).summary.covered_fields == 0
    path = f'/api/runs/{run.id}/field-evidence'
    response = await http_client.get(path)
    assert response.status_code == 200, response.text
    assert response.json()['requestCount'] == 8
    assert response.json()['fields'][2]['unavailableCount'] == 3
    for offset, length in ((0, 2), (2, 1), (100, 0)):
        page = (await http_client.get(path, params={'limit': 2, 'offset': offset})).json()
        assert page['total'] == 3 and len(page['fields']) == length
    filtered = (await http_client.get(path, params={'typeName': 'User', 'fieldName': 'name', 'state': 'not_observed'})).json()
    assert filtered['total'] == 1 and filtered['fields'][0]['observedHits'] == 1
    detail = await http_client.get(f'/api/runs/{run.id}/requests/{ids[0]}')
    assert detail.json()['fieldEvidenceVersion'] == 1
    detail = await http_client.get(f'/api/runs/{run.id}/requests/{legacy}')
    assert detail.json()['fieldEvidenceVersion'] is None
    one = (await http_client.get(path, params={'requestId': str(legacy)})).json()
    assert one['requestCount'] == 1 and one['fields'][0]['unavailableCount'] == 1
    assert (await http_client.get(f'/api/runs/{other.id}/field-evidence', params={'requestId': str(ids[0])})).status_code == 404
    assert (await http_client.get(path, params={'requestId': str(uuid.uuid4())})).status_code == 404


@pytest.mark.parametrize('query,payload,expected', [
    ('{ users { name } }', {'data': {'users': []}}, 'indeterminate'),
    ('{ users { name } }', {'data': {'users': [None, {'name': None}, {}]}}, 'observed'),
    ('{ a: users { n: name } b: users { name } }', {'data': {'a': [{'n': None}], 'b': []}}, 'observed'),
    ('query($yes: Boolean = false) { users { name @include(if: $yes) } }', {'data': {'users': [{}]}}, None),
])
async def test_lists_aliases_directives_no_double_count(query, payload, expected, postgres_session):
    service = await create_service(postgres_session, 'evidence-' + uuid.uuid4().hex, 'Lists')
    snapshot = await import_sdl(postgres_session, service.id, SDL)
    run = await create_run(postgres_session, snapshot.id, 'Evidence', {})
    ident = await capture(postgres_session, run, payload, query)
    rows = (await postgres_session.execute(select(RequestFieldEvidence.state).join(
        SchemaField, SchemaField.id == RequestFieldEvidence.field_id).where(
        RequestFieldEvidence.request_id == ident, SchemaField.name == 'name'))).scalars().all()
    assert rows == ([] if expected is None else [expected])
    dto = await get_field_evidence(postgres_session, run.id, EvidenceFilters())
    assert all(f.selected_hits == 1 and f.selected_request_count == 1 for f in dto.fields)


async def test_real_fragment_analyzer_records_different_runtime_types(postgres_session):
    service = await create_service(postgres_session, 'evidence-' + uuid.uuid4().hex, 'Fragments')
    snapshot = await import_sdl(postgres_session, service.id, SDL)
    run = await create_run(postgres_session, snapshot.id, 'Fragments', {})
    query = '{ search { kind: __typename ...U ... on Order { sum: total } } } fragment U on User { display: name }'
    for objects in ([{'kind': 'User', 'display': None}], [{'display': 'unknown type'}],
                    [{'kind': 'User', 'display': 'x'}, {'kind': 'Order', 'sum': 3}]):
        await capture(postgres_session, run, {'data': {'search': objects}}, query)
    fields = {f.type_name: f for f in (await get_field_evidence(postgres_session, run.id, EvidenceFilters())).fields}
    assert fields['User'].observed_hits == 2 and fields['User'].indeterminate_count == 1
    assert fields['Order'].observed_hits == fields['Order'].indeterminate_count == fields['Order'].not_observed_count == 1
    assert all(f.selected_hits == 3 for f in fields.values())


async def test_real_proxy_does_not_change_wire_query_or_store_values(postgres_session, http_client, upstream, monkeypatch, caplog):
    run, _, _ = await make_run(postgres_session)
    monkeypatch.setattr(app, 'state', State({'coverage_analyzer': analyze_e2e_query}))
    wire = b'{"data":{"person":{"label":null,"id":"secret-response-value"}}}'
    calls = upstream(lambda request: raw_response(200, wire))
    body = b'{"query":"{ person: user { label: name id } }","variables":{"token":"secret-variable-token"}}'
    response = await http_client.post('/graphql', content=body, headers={'X-Coverage-Run-Id': str(run.id)})
    assert response.content == wire and calls[0].content == body and b'__typename' not in calls[0].content
    path = f'/api/runs/{run.id}/field-evidence'
    result = await http_client.get(path)
    assert all(f['observedHits'] == 1 for f in result.json()['fields'])
    stored = await postgres_session.scalar(text('SELECT jsonb_agg(to_jsonb(e))::text FROM request_field_evidence e'))
    for value in ('secret-response-value', 'secret-variable-token', 'person', 'label'):
        assert value not in stored + result.text + caplog.text
    row = (await postgres_session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == run.id))).one()
    row_json = await postgres_session.scalar(text('SELECT to_jsonb(r)::text FROM observed_requests r WHERE id=:id'), {'id': row.id})
    assert 'secret-response-value' not in row_json and 'secret-variable-token' not in row_json


@pytest.mark.parametrize('body', [None, b'not json', b'[]', b'{"data":{}}' + b' ' * 1_048_576])
async def test_invalid_or_large_response_persists_unavailable(postgres_session, body):
    run, _, _ = await make_run(postgres_session)
    await capture(postgres_session, run, body)
    page = await get_field_evidence(postgres_session, run.id, EvidenceFilters())
    assert all(f.unavailable_count == 1 and f.selected_hits == f.observed_hits == 0 for f in page.fields)


async def test_historical_and_failed_analyzer_have_no_invented_fields(postgres_session):
    run, _, _ = await make_run(postgres_session)
    await capture(postgres_session, run, {'data': {'user': {'name': 'x'}}}, 'not graphql')
    await capture(postgres_session, run, {'data': {'user': {'id': '1'}}}, '{ user { ...Missing } }')
    page = await get_field_evidence(postgres_session, run.id, EvidenceFilters())
    assert page.fields == [] and page.selection_unavailable_request_count == page.evidence_unavailable_request_count == 2


@pytest.mark.parametrize('role', ['viewer', 'analyst', 'operator', 'admin'])
async def test_evidence_acl_requires_role_and_explicit_service_grant(role, auth_context, postgres_session):
    client, users, private, public, _, _, private_run, public_run = auth_context
    private_path = f'/api/runs/{private_run.id}/field-evidence'
    public_path = f'/api/runs/{public_run.id}/field-evidence'
    assert (await client.get(public_path)).status_code == 401
    await login(client, role)
    expected = 200 if role == 'admin' else 403 if role == 'viewer' else 404
    assert (await client.get(private_path)).status_code == expected
    assert (await client.get(public_path)).status_code == expected
    await grant(postgres_session, users[role], private)
    assert (await client.get(private_path)).status_code == (403 if role == 'viewer' else 200)
    # A grant for one service cannot disclose the other service, even public.
    assert (await client.get(public_path)).status_code == expected


@pytest.mark.parametrize('params', [{'state': 'bad'}, {'limit': 0}, {'limit': 501}, {'offset': -1},
                                    {'requestId': 'bad'}, {'typeName': ''}])
async def test_api_filter_validation(params, http_client):
    response = await http_client.get(f'/api/runs/{uuid.uuid4()}/field-evidence', params=params)
    assert response.status_code == 422


async def test_invalid_evidence_and_foreign_snapshot_are_rejected_before_write(postgres_session):
    run, _, _ = await make_run(postgres_session)
    base = RequestObservationInput(run_id=run.id,
        analysis=AnalysisResult(status='success', fields=(FieldReference('User', 'name'),)),
        http_method='POST', http_status=200, response_has_data=True, graphql_has_errors=False)
    for evidence in ((), (FieldEvidence(FieldReference('User', 'id'), 'observed'),),
                     (FieldEvidence(FieldReference('User', 'name'), 'bad'),),
                     (FieldEvidence(FieldReference('User', 'name'), 'observed'),) * 2):
        with pytest.raises(InvalidReferenceError):
            await record_observation(postgres_session, replace(base, field_evidence=evidence))
    with pytest.raises(InvalidReferenceError):
        await record_observation(postgres_session, replace(base, response_has_data=False,
            field_evidence=(FieldEvidence(FieldReference('User', 'name'), 'observed'),)))
    assert await postgres_session.scalar(select(func.count()).select_from(ObservedRequest)) == 0
    # The composite FK prevents attaching evidence to unselected/foreign fields.
    ident = await capture(postgres_session, run, {'data': {'user': {'name': 'x'}}})
    other_field = await postgres_session.scalar(select(SchemaField.id).join(SchemaType, SchemaType.id == SchemaField.type_id).where(
        SchemaType.snapshot_id != run.snapshot_id))
    with pytest.raises(IntegrityError):
        async with postgres_session.begin_nested():
            postgres_session.add(RequestFieldEvidence(request_id=ident, field_id=other_field, state='observed'))
            await postgres_session.flush()
    await finish_run(postgres_session, run.id)
    with pytest.raises(ConflictError):
        await capture(postgres_session, run, {'data': {'user': {'name': 'x'}}})


async def test_evidence_insert_failure_rolls_back_request_and_usage(isolated_database):
    async with isolated_database() as session:
        service = await create_service(session, 'atomic', 'Atomic')
        snapshot = await import_sdl(session, service.id, 'type Query { x: Int }')
        run = await create_run(session, snapshot.id, 'Atomic', {})
        await session.execute(text("CREATE FUNCTION reject_evidence() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test-only'; END $$"))
        await session.execute(text('CREATE TRIGGER reject_evidence BEFORE INSERT ON request_field_evidence FOR EACH ROW EXECUTE FUNCTION reject_evidence()'))
        await session.commit()
        with pytest.raises(DBAPIError):
            await capture(session, run, {'data': {'x': None}}, '{ x }')
        await session.rollback()
        for model in (ObservedRequest, RequestFieldUsage, RequestFieldEvidence):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_upgrade_downgrade_preserves_legacy_usage_and_coverage(isolated_database):
    engine = isolated_database.kw['bind']
    url = engine.url.render_as_string(hide_password=False)
    async def migrate(*args):
        result = await asyncio.to_thread(subprocess.run, [sys.executable, '-m', 'alembic', *args],
            cwd=ROOT, env={**os.environ, 'DATABASE_URL': url}, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    async with isolated_database() as session:
        service = await create_service(session, 'migration', 'Migration')
        snapshot = await import_sdl(session, service.id, 'type Query { x: Int }')
        run = await create_run(session, snapshot.id, 'Legacy', {})
        await capture(session, run, {'data': {'x': None}}, '{ x }')
        before = (await get_run_graph(session, run.id)).model_dump()
    await migrate('downgrade', '20261121_gitlab_pipelines')
    await migrate('upgrade', 'head')
    await migrate('check')
    async with isolated_database() as session:
        assert (await get_run_graph(session, run.id)).model_dump() == before
        page = await get_field_evidence(session, run.id, EvidenceFilters())
        assert page.fields[0].unavailable_count == page.fields[0].selected_hits == 1
        assert page.fields[0].observed_hits == 0
    await migrate('downgrade', '20261121_gitlab_pipelines')
    await migrate('upgrade', 'head')


async def test_real_proxy_preserves_wire_bytes_and_persists_only_presence(
        postgres_session, http_client, upstream, monkeypatch, caplog):
    run, _, _ = await make_run(postgres_session)
    monkeypatch.setattr(app, 'state', State({'coverage_analyzer': analyze_e2e_query}))
    canaries = ['private-response-value-431', 'private-variable-token-432']
    query = '{ a: user { n: name n: name } }'
    body = json.dumps({'query': query, 'variables': {'token': canaries[1]}}).encode()
    wire = json.dumps({'data': {'a': {'n': canaries[0]}}}).encode()
    calls = upstream(lambda request: raw_response(200, wire))
    response = await http_client.post('/graphql', content=body, headers={'X-Coverage-Run-Id': str(run.id)})
    assert response.content == wire and len(calls) == 1
    assert calls[0].content == body and '__typename' not in calls[0].content.decode()
    row = (await postgres_session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == run.id))).one()
    saved_request = await postgres_session.scalar(text('SELECT to_jsonb(r)::text FROM observed_requests r WHERE id=:id'), {'id': row.id})
    saved_evidence = (await postgres_session.scalars(text('SELECT to_jsonb(e)::text FROM request_field_evidence e WHERE request_id=:id'), {'id': row.id})).all()
    assert len(saved_evidence) == 2
    assert all(set(json.loads(e)) == {'request_id', 'field_id', 'state'} for e in saved_evidence)
    dto = await http_client.get(f'/api/runs/{run.id}/field-evidence')
    for secret in canaries:
        assert secret not in saved_request + repr(saved_evidence) + dto.text + caplog.text
    assert all(f['observedHits'] == f['selectedHits'] == 1 for f in dto.json()['fields'])


async def test_missing_evidence_row_falls_back_without_erasing_selected_hits(postgres_session):
    run, _, _ = await make_run(postgres_session)
    ident = await capture(postgres_session, run, {'data': {'user': {'id': None, 'name': None}}})
    await postgres_session.execute(delete(RequestFieldEvidence).where(RequestFieldEvidence.request_id == ident))
    await postgres_session.commit()
    page = await get_field_evidence(postgres_session, run.id, EvidenceFilters())
    assert all(f.unavailable_count == f.selected_hits == 1 and f.observed_hits == 0 for f in page.fields)
    assert page.evidence_unavailable_request_count == 1


@pytest.mark.parametrize('body', [None, b'not-json', b'{"data":[]}', b'{"data":{}}' + b' ' * 1_048_576])
async def test_unusable_response_never_reports_negative_evidence(body, postgres_session):
    run, _, _ = await make_run(postgres_session)
    await capture(postgres_session, run, body)
    page = await get_field_evidence(postgres_session, run.id, EvidenceFilters())
    assert page.evidence_unavailable_request_count == 1
    assert len(page.fields) == 3
    assert all(f.unavailable_count == 1 and f.selected_hits == f.observed_hits == f.not_observed_count == 0
               for f in page.fields)
