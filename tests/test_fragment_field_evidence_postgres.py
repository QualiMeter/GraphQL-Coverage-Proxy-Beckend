"""Real documents → transparent proxy → capture → PostgreSQL → evidence API."""
import json
import uuid

import pytest
from sqlalchemy import func, select, text
from starlette.datastructures import State

from app.analyzer_adapter import analyze_e2e_query
from app.api_models import RunComparisonRequest
from app.auth import Principal
from app.contracts import AnalysisResult, FieldReference, RequestObservationInput
from app.evidence_analytics import EvidenceFilters, get_field_evidence
from app.main import app
from app.models import ObservedRequest, RequestFieldEvidence, RequestFieldUsage, SchemaField
from app.run_analytics import get_run_stats
from app.run_comparison import compare_run_series
from app.services import create_run, create_service, finish_run, get_run_graph, import_sdl, record_observation
from test_e2e_capture_postgres import postgres_session  # noqa: F401
from test_graphql_proxy import upstream, raw_response  # noqa: F401
from test_proxy_e2e_postgres import http_client  # noqa: F401
from test_fragment_field_evidence import SDL, SEARCH, NESTED

pytestmark = pytest.mark.integration


async def make_run(session, sdl=SDL):
    service = await create_service(session, 'fragments-' + uuid.uuid4().hex, 'Fragment evidence',
                                   endpoint_url='https://upstream.example/graphql')
    snapshot = await import_sdl(session, service.id, sdl)
    run = await create_run(session, snapshot.id, 'Fragments', {})
    return run, snapshot


async def exchange(client, upstream, run, query, payload, variables=None):
    wire = json.dumps(payload).encode()
    body = json.dumps({'query': query, 'variables': variables or {}}).encode()
    calls = upstream(lambda request: raw_response(200, wire))
    before = len(calls)
    response = await client.post('/graphql', content=body, headers={
        'Content-Type': 'application/json', 'X-Coverage-Run-Id': str(run.id)})
    assert response.status_code == 200, response.text
    assert response.content == wire and len(calls) == before + 1 and calls[-1].content == body
    result = await client.get(f'/api/runs/{run.id}/field-evidence')
    assert result.status_code == 200, result.text
    return result.json()


def field_map(page):
    return {f'{f["typeName"]}.{f["fieldName"]}': f for f in page['fields']}


@pytest.fixture
def real_analyzer(monkeypatch):
    monkeypatch.setattr(app, 'state', State({'coverage_analyzer': analyze_e2e_query}))


@pytest.mark.parametrize('query', [SEARCH,
    'query Search { search { __typename ...UserDetails ...OrderDetails } } fragment UserDetails on User { name } fragment OrderDetails on Order { total }'])
async def test_user_example_inline_and_named_proxy_to_evidence_api(query, postgres_session, http_client, upstream, real_analyzer):
    run, snapshot = await make_run(postgres_session)
    page = await exchange(http_client, upstream, run, query, {'data': {'search': {'__typename': 'User', 'name': 'Alex'}}})
    fields = field_map(page)
    assert page['snapshotId'] == str(snapshot.id) and page['requestCount'] == 1
    assert fields['User.name']['observedHits'] == 1
    assert fields['Order.total']['observedHits'] == 0 and fields['Order.total']['notObservedCount'] == 1
    assert all(f['selectedRequestCount'] == f['selectedHits'] == 1 for f in fields.values())
    assert all('__typename' not in coordinate for coordinate in fields)
    stored = (await postgres_session.execute(select(SchemaField.name, RequestFieldEvidence.state).join(
        RequestFieldEvidence, RequestFieldEvidence.field_id == SchemaField.id))).all()
    assert set(stored) == {('search', 'observed'), ('name', 'observed'), ('total', 'not_observed')}
    assert await postgres_session.scalar(select(func.count()).select_from(RequestFieldUsage)) == 3
    row = (await postgres_session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == run.id))).one()
    assert row.analysis_status == 'success' and row.field_evidence_version == 1
    detail = (await http_client.get(f'/api/runs/{run.id}/requests/{row.id}')).json()
    assert detail['countedInCoverage'] and len(detail['selectedFields']) == 3
    per_request = (await http_client.get(f'/api/runs/{run.id}/field-evidence', params={'requestId': str(row.id)})).json()
    assert per_request['fields'] == page['fields']
    filtered = (await http_client.get(f'/api/runs/{run.id}/field-evidence', params={'state': 'not_observed', 'limit': 1})).json()
    assert filtered['total'] == 1 and filtered['fields'][0]['typeName'] == 'Order'


@pytest.mark.parametrize('objects,errors,name,total,friend', [
    ([{'kind': 'User', 'key': 'u', 'display': None, 'friend': {'label': 'Alex'}}, {'kind': 'Order', 'key': 'o', 'sum': None}], [], 'observed', 'observed', 'observed'),
    ([], [], 'indeterminate', 'indeterminate', 'indeterminate'),
    ([None, {'kind': 'User', 'key': 'u', 'display': None, 'friend': None}], [], 'observed', 'indeterminate', 'observed'),
    ([{'key': 'u', 'display': 'Alex'}], [], 'indeterminate', 'indeterminate', 'indeterminate'),
    ([{'kind': 'Unknown', 'display': 'Alex'}], [], 'indeterminate', 'indeterminate', 'indeterminate'),
    ([{'kind': 'User', 'key': 'u', 'friend': None}], [{}], 'indeterminate', 'not_observed', 'observed'),
    (None, [{}], 'indeterminate', 'indeterminate', 'indeterminate'),
])
async def test_nested_fragments_aliases_repeated_spreads_arrays_and_partial_data(
        objects, errors, name, total, friend, postgres_session, http_client, upstream, real_analyzer):
    run, _ = await make_run(postgres_session)
    page = await exchange(http_client, upstream, run, NESTED, {'data': {'results': objects}, 'errors': errors})
    fields = field_map(page)
    for coordinate, expected in [('User.name', name), ('Order.total', total), ('User.friend', friend)]:
        counter = {'observed': 'observedHits', 'not_observed': 'notObservedCount', 'indeterminate': 'indeterminateCount'}[expected]
        assert fields[coordinate][counter] == 1, (coordinate, fields[coordinate])
    assert all(f['selectedRequestCount'] == f['selectedHits'] == 1 for f in fields.values())
    assert await postgres_session.scalar(select(func.count()).select_from(RequestFieldUsage)) == len(fields) == 5
    assert await postgres_session.scalar(select(func.count()).select_from(RequestFieldEvidence)) == 5
    graph = await get_run_graph(postgres_session, run.id)
    # Node.id is an interface coordinate; only OBJECT coordinates contribute.
    assert graph.summary.covered_fields == 4
    stats = await get_run_stats(postgres_session, run.id)
    assert stats.coverage_percent == graph.summary.coverage_percent


@pytest.mark.parametrize('variables,selected', [
    ({}, {'Query.results', 'Node.id', 'User.name', 'User.friend', 'Order.total'}),
    ({'include': False}, {'Query.results', 'Node.id', 'Order.total'}),
    ({'skip': True}, {'Query.results', 'Node.id', 'User.name', 'User.friend'}),
    ({'include': False, 'skip': True}, {'Query.results', 'Node.id'}),
])
async def test_runtime_directives_through_real_proxy(variables, selected, postgres_session, http_client, upstream, real_analyzer):
    run, _ = await make_run(postgres_session)
    page = await exchange(http_client, upstream, run, NESTED, {'data': {'results': []}}, variables)
    assert set(field_map(page)) == selected
    assert page['selectionUnavailableRequestCount'] == 0


async def test_repeated_fragment_at_multiple_response_objects_is_not_lost(postgres_session, http_client, upstream, real_analyzer):
    run, _ = await make_run(postgres_session)
    query = '{ a: user { ...Names } b: user { ...Names friend { ...Names } } } fragment Names on User { name }'
    page = await exchange(http_client, upstream, run, query, {'data': {'a': {}, 'b': {'name': None, 'friend': None}}})
    fields = field_map(page)
    assert fields['User.name']['observedHits'] == 1
    assert all(f['selectedHits'] == 1 for f in fields.values())


async def test_merged_abstract_selections_share_explicit_typename(postgres_session, http_client, upstream, real_analyzer):
    run, _ = await make_run(postgres_session)
    query = '{ results { kind: __typename } results { ...U ... on Order { total } } } fragment U on User { name }'
    page = await exchange(http_client, upstream, run, query, {'data': {'results': [{'kind': 'User', 'name': None}]}})
    fields = field_map(page)
    assert fields['User.name']['observedHits'] == 1 and fields['Order.total']['notObservedCount'] == 1


async def test_actual_fragment_privacy_and_wire_preservation(postgres_session, http_client, upstream, real_analyzer, caplog):
    run, _ = await make_run(postgres_session, SDL.replace('search: Search', 'search(term: String): Search'))
    query = 'query Search($term: String) { search(term: $term) { __typename ...Details } } fragment Details on User { name }'
    wire = b'{"data":{"search":{"__typename":"User","name":"fragment-user-secret"}},"errors":[{"message":"fragment-error-secret"}]}'
    body = json.dumps({'query': query, 'variables': {'term': 'fragment-variable-secret'}}).encode()
    calls = upstream(lambda request: raw_response(200, wire))
    response = await http_client.post('/graphql', content=body, headers={
        'X-Coverage-Run-Id': str(run.id), 'Authorization': 'Bearer fragment-token-secret'})
    assert response.content == wire and calls[0].content == body
    assert calls[0].headers['authorization'] == 'Bearer fragment-token-secret'
    row = (await postgres_session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == run.id))).one()
    stored_request = await postgres_session.scalar(text('SELECT to_jsonb(r)::text FROM observed_requests r WHERE id=:id'), {'id': row.id})
    stored_evidence = await postgres_session.scalar(text('SELECT jsonb_agg(to_jsonb(e))::text FROM request_field_evidence e WHERE request_id=:id'), {'id': row.id})
    dto = await http_client.get(f'/api/runs/{run.id}/field-evidence')
    detail = await http_client.get(f'/api/runs/{run.id}/requests/{row.id}')
    assert row.analysis_status == 'success' and field_map(dto.json())['User.name']['observedHits'] == 1
    for secret in ['fragment-user-secret', 'fragment-error-secret', 'fragment-variable-secret', 'fragment-token-secret']:
        assert secret not in stored_request + stored_evidence + dto.text + detail.text + caplog.text


async def test_pr34_comparison_uses_selected_hits_despite_different_evidence(postgres_session, http_client, upstream, real_analyzer):
    run1, snapshot = await make_run(postgres_session)
    await exchange(http_client, upstream, run1, SEARCH, {'data': {'search': {'__typename': 'User', 'name': 'Alex'}}})
    await finish_run(postgres_session, run1.id)
    run2 = await create_run(postgres_session, snapshot.id, 'Second', {})
    await exchange(http_client, upstream, run2, SEARCH, {'data': {'search': {'__typename': 'User'}}, 'errors': [{}]})
    await finish_run(postgres_session, run2.id)
    comparison = await compare_run_series(postgres_session, RunComparisonRequest(
        run_ids=[run1.id, run2.id], baseline_run_id=run1.id, target_run_id=run2.id), Principal(role='admin'))
    assert comparison.comparison.percentage_point_delta == comparison.comparison.covered_fields_delta == 0
    for run in (run1, run2):
        graph = await get_run_graph(postgres_session, run.id)
        compared = next(r for r in comparison.runs if r.run_id == run.id)
        assert graph.summary.coverage_percent == compared.coverage_percent
        page = await get_field_evidence(postgres_session, run.id, EvidenceFilters())
        assert all(f.selected_hits == 1 for f in page.fields)
    name = next(f for f in comparison.fields.items if f.type_name == 'User' and f.field_name == 'name')
    assert [h.hits for h in name.hits_by_run] == [1, 1]
    before = next(f for f in (await get_field_evidence(postgres_session, run1.id, EvidenceFilters())).fields if f.field_name == 'name')
    after = next(f for f in (await get_field_evidence(postgres_session, run2.id, EvidenceFilters())).fields if f.field_name == 'name')
    assert before.observed_hits == 1 and after.observed_hits == 0 and after.indeterminate_count == 1


async def test_historical_request_without_evidence_stays_unavailable_after_fragments_merge(postgres_session, http_client, real_analyzer):
    run, _ = await make_run(postgres_session)
    ident = await record_observation(postgres_session, RequestObservationInput(
        run_id=run.id, analysis=AnalysisResult(status='success', fields=(FieldReference('User', 'name'),)),
        http_method='POST', http_status=200, response_has_data=True, graphql_has_errors=False))
    dto = (await http_client.get(f'/api/runs/{run.id}/field-evidence', params={'requestId': str(ident)})).json()
    assert dto['fields'][0]['unavailableCount'] == dto['fields'][0]['selectedHits'] == 1
    assert dto['fields'][0]['observedHits'] == 0
