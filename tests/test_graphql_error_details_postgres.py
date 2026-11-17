"""Real upstream exchange → PostgreSQL → protected detail, with privacy canaries."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import State

from app import e2e_capture
from app.analyzer_adapter import analyze_e2e_query
from app.auth_models import ServiceGrant
from app.contracts import AnalysisResult, RequestObservationInput
from app.graphql_errors import ErrorSummary
from app.main import app
from app.models import ObservedRequest
from app.services import record_observation
from test_graphql_error_details import CANARIES
from test_graphql_proxy import raw_response, upstream  # noqa: F401
from test_e2e_capture_postgres import postgres_session  # noqa: F401
from test_proxy_e2e_postgres import http_client, make_run  # noqa: F401
from test_browser_auth_postgres import auth_context, grant, login, TEST_KEY  # noqa: F401
from test_multi_service_postgres import isolated_database  # noqa: F401

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('status,payload,count,availability,has_errors', [
    (200, {'data': {'user': {'id': '1'}}}, 0, 'sanitized', False),
    (200, {'errors': [{'path': ['user', 'id']}]}, 1, 'sanitized', True),
    (200, {'data': {'user': None}, 'errors': [{}, {}]}, 2, 'sanitized', True),
    (500, {'data': None}, 0, 'sanitized', False),
    (500, {'errors': []}, 0, 'sanitized', False),
    (200, {'errors': 'wrong'}, None, 'unavailable', True),
    (200, {'errors': [None]}, None, 'unavailable', True),
    (200, {'errors': [{}] * 32}, 32, 'truncated', True),
    (503, 'not json', None, 'unavailable', None),
])
async def test_response_metadata_roundtrip(status, payload, count, availability, has_errors,
        postgres_session, http_client, upstream, monkeypatch):
    run, _, other_run = await make_run(postgres_session)
    monkeypatch.setattr(app, 'state', State({'coverage_analyzer': analyze_e2e_query}))
    wire = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
    calls = upstream(lambda request: raw_response(status, wire))
    response = await http_client.post('/graphql', json={'query': '{ user { id } }'},
                                     headers={'X-Coverage-Run-Id': str(run.id)})
    assert response.status_code == status and response.content == wire and len(calls) == 1
    row = (await postgres_session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == run.id))).one()
    assert (row.graphql_has_errors, row.graphql_error_count, row.graphql_error_details_availability) == (
        has_errors, count, availability)
    detail = (await http_client.get(f'/api/runs/{run.id}/requests/{row.id}')).json()
    assert detail['graphqlErrorCount'] == count
    assert detail['graphqlErrorDetailsAvailability'] == availability
    assert detail['graphqlErrorDetails'] == row.graphql_error_details
    stats = (await http_client.get(f'/api/runs/{run.id}/stats')).json()
    operations = (await http_client.get(f'/api/runs/{run.id}/operations')).json()
    assert stats['graphqlErrorCount'] == int(has_errors is True)
    assert operations[0]['graphqlErrorCount'] == int(has_errors is True)
    assert 'graphqlErrorDetails' not in json.dumps([stats, operations])
    assert detail['countedInCoverage'] == (isinstance(payload, dict) and isinstance(payload.get('data'), dict))
    assert (await http_client.get(f'/api/runs/{other_run.id}/requests/{row.id}')).status_code == 404
    assert (await http_client.get(f'/api/runs/{run.id}/requests/{uuid.uuid4()}')).status_code == 404
    listing = await http_client.get(f'/api/runs/{run.id}/requests')
    assert 'graphqlErrorDetails' not in listing.text and 'graphqlErrorCount' not in listing.text


async def test_canaries_absent_in_actual_postgres_row_dto_logs_and_diagnostics(
        postgres_session, http_client, upstream, monkeypatch, caplog, capfd):
    run, _, _ = await make_run(postgres_session)
    monkeypatch.setattr(app, 'state', State({'coverage_analyzer': analyze_e2e_query}))
    payload = {'data': {'user': {'id': ' '.join(CANARIES)}}, 'errors': [
        {'message': secret, 'path': ['user', secret], 'extensions': {
            'code': secret, 'exception': {'stacktrace': [secret]}, 'variables': CANARIES},
         'locations': [{'line': secret, 'column': 1}]} for secret in CANARIES]}
    payload['errors'].append({'message': ' '.join(CANARIES),
        'extensions': {'code': 'BAD_USER_INPUT', 'private': CANARIES}, 'path': ['user', 'id']})
    wire = json.dumps(payload).encode()
    calls = upstream(lambda request: raw_response(200, wire))
    response = await http_client.post('/graphql', json={'query': '{ user { id } }'},
                                     headers={'X-Coverage-Run-Id': str(run.id)})
    assert response.content == wire and len(calls) == 1
    row = (await postgres_session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == run.id))).one()
    # Read raw SQL JSON for the entire stored row, not mock arguments or repr(ORM).
    stored = await postgres_session.scalar(text('SELECT to_jsonb(r)::text FROM observed_requests r WHERE id=:id'), {'id': row.id})
    detail = await http_client.get(f'/api/runs/{run.id}/requests/{row.id}')
    assert detail.status_code == 200
    assert detail.json()['graphqlErrorCount'] == len(CANARIES) + 1
    assert detail.json()['graphqlErrorDetails'][-1]['path'] == ['user', 'id']
    assert detail.json()['graphqlErrorDetails'][0]['safeMessage'] is None
    logs = caplog.text + repr([record.__dict__ for record in caplog.records])
    stderr = capfd.readouterr().err
    for secret in CANARIES:
        assert secret not in stored + detail.text + logs + stderr


async def test_optional_sanitizer_failure_still_commits_successful_coverage(
        postgres_session, http_client, upstream, monkeypatch, caplog, capfd):
    run, _, _ = await make_run(postgres_session)
    monkeypatch.setattr(app, 'state', State({'coverage_analyzer': analyze_e2e_query}))
    def broken(*args):
        raise RuntimeError(' '.join(CANARIES))
    monkeypatch.setattr(e2e_capture, 'sanitize_errors', broken)
    wire = b'{"data":{"user":{"id":"1"}},"errors":[{}]}'
    calls = upstream(lambda request: raw_response(200, wire))
    response = await http_client.post('/graphql', json={'query': '{ user { id } }'},
                                     headers={'X-Coverage-Run-Id': str(run.id)})
    assert response.content == wire and len(calls) == 1
    row = (await postgres_session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == run.id))).one()
    assert row.graphql_has_errors is True and row.response_has_data is True
    assert row.graphql_error_count == 1 and row.graphql_error_details is None
    assert row.graphql_error_details_availability == 'unavailable'
    detail = (await http_client.get(f'/api/runs/{run.id}/requests/{row.id}')).json()
    assert detail['countedInCoverage'] is True
    coverage = (await http_client.get(f'/api/runs/{run.id}/coverage')).json()
    assert coverage['summary']['coveredFields'] == 2
    assert not caplog.records and not capfd.readouterr().err


@pytest.mark.parametrize('role', ['anonymous', 'viewer', 'analyst', 'operator', 'admin', 'cli'])
async def test_error_details_require_role_and_live_grant(role, auth_context, postgres_session):
    client, users, private, public, _, _, private_run, public_run = auth_context
    rows = []
    for run in (private_run, public_run):
        row_id = await record_observation(postgres_session, RequestObservationInput(run_id=run.id,
            analysis=AnalysisResult(status='success'), http_method='POST', http_status=200,
            response_has_data=True, graphql_has_errors=True,
            graphql_errors=ErrorSummary(1, 'sanitized', [
                {'code': None, 'path': None, 'locations': [], 'safeMessage': None}])))
        rows.append(row_id)
    headers = {}
    if role == 'cli':
        headers = {'X-Coverage-Api-Key': TEST_KEY}
    elif role != 'anonymous':
        await login(client, role)
    paths = [f'/api/runs/{run.id}/requests/{row}' for run, row in zip((private_run, public_run), rows)]
    for path in paths:
        result = await client.get(path, headers=headers)
        assert result.status_code == (401 if role == 'anonymous' else 403 if role == 'viewer'
                                      else 200 if role in {'admin', 'cli'} else 404)
    if role not in {'anonymous', 'cli'}:
        for service in (private, public):
            await grant(postgres_session, users[role], service)
        for path in paths:
            result = await client.get(path)
            assert result.status_code == (403 if role == 'viewer' else 200)
            if result.status_code == 200:
                assert result.json()['graphqlErrorCount'] == 1
        await postgres_session.execute(delete(ServiceGrant).where(ServiceGrant.user_id == users[role].id))
        await postgres_session.commit()
        for path in paths:
            assert (await client.get(path)).status_code == (403 if role == 'viewer' else 200 if role == 'admin' else 404)
    if role in {'admin', 'cli'}:
        assert (await client.get(f'/api/runs/{private_run.id}/requests/{rows[1]}', headers=headers)).status_code == 404


@pytest.mark.parametrize('count,availability,details,has_errors', [
    (-1, 'unavailable', None, True), (None, 'invalid', None, True),
    (1, 'sanitized', None, True), (1, 'sanitized', {}, True),
    (1, 'sanitized', [], True), (0, 'sanitized', [], True),
    (1, 'unavailable', [], True), (21, 'truncated', [{}] * 21, True),
    (1, 'sanitized', [{'code': 'x' * 17000}], True),
])
async def test_database_rejects_invalid_metadata_atomically(count, availability, details, has_errors, postgres_session):
    run, _, _ = await make_run(postgres_session)
    with pytest.raises(IntegrityError):
        async with postgres_session.begin_nested():
            await record_observation(postgres_session, RequestObservationInput(run_id=run.id,
                analysis=AnalysisResult(status='success'), http_method='POST', http_status=200,
                response_has_data=True, graphql_has_errors=has_errors,
                graphql_errors=ErrorSummary(count, availability, details)))
    assert (await postgres_session.scalars(select(ObservedRequest).where(ObservedRequest.run_id == run.id))).all() == []


async def test_additive_migration_preserves_historical_error_flags(isolated_database):
    engine = isolated_database.kw['bind']
    url = engine.url.render_as_string(hide_password=False)
    async def migrate(*args):
        result = await asyncio.to_thread(subprocess.run, [sys.executable, '-m', 'alembic', *args],
            cwd=ROOT, env={**os.environ, 'DATABASE_URL': url}, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    await migrate('downgrade', '20261119_operation_name_text')
    service, snapshot, run = [uuid.uuid4() for _ in range(3)]
    ids = [uuid.uuid4() for _ in range(3)]
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO graphql_services(id,key,name) VALUES (:id,'legacy','Legacy')"), {'id': service})
        await conn.execute(text("INSERT INTO schema_snapshots(id,service_id,schema_hash,schema_sdl,source) VALUES (:id,:service,:hash,'type Query { x: Int }','sdl')"),
                           {'id': snapshot, 'service': service, 'hash': 'a' * 64})
        await conn.execute(text("INSERT INTO test_runs(id,snapshot_id,name,status) VALUES (:id,:snapshot,'Legacy','completed')"), {'id': run, 'snapshot': snapshot})
        for row_id, flag in zip(ids, (True, False, None)):
            await conn.execute(text("INSERT INTO observed_requests(id,run_id,http_method,analysis_status,graphql_has_errors) VALUES (:id,:run,'POST','success',:flag)"),
                               {'id': row_id, 'run': run, 'flag': flag})
        before = (await conn.execute(text('SELECT to_jsonb(r) FROM observed_requests r ORDER BY id'))).scalars().all()
    await migrate('upgrade', 'head')
    await migrate('check')
    async with engine.connect() as conn:
        after = (await conn.execute(text('SELECT to_jsonb(r) FROM observed_requests r ORDER BY id'))).scalars().all()
    for old, new in zip(before, after):
        assert new.pop('test_execution_id') is None
        assert new.pop('field_evidence_version') is None
        assert new.pop('graphql_error_count') is None
        assert new.pop('graphql_error_details') is None
        assert new.pop('graphql_error_details_availability') == 'unavailable'
        assert old == new
    from app.journal import get_request_detail
    async with isolated_database() as session:
        for row_id in ids:
            dto = await get_request_detail(session, run, row_id)
            assert dto.graphql_error_count is None and dto.graphql_error_details is None
            assert dto.graphql_error_details_availability == 'unavailable'
    await migrate('downgrade', '20261119_operation_name_text')
    await migrate('upgrade', 'head')
