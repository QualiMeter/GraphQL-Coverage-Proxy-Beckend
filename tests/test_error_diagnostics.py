"""Observe real error boundaries without formatting hostile exceptions or input."""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.exc import DBAPIError

from app import api, e2e_capture, proxy, query_analyzer, safe_document, schema_parser
from app.analyzer_adapter import analyze_e2e_query
from app.main import app
from test_graphql_proxy import client, raw_response, upstream, TEST_ADMIN_KEY  # noqa: F401
from test_proxy_e2e import capture_context  # noqa: F401

SECRETS = ('cookie-secret', 'token-secret', 'password-secret', 'variable-secret',
           'request-body-secret', 'response-body-secret', 'operation-name-secret',
           'query-string-secret', TEST_ADMIN_KEY)
BODY = {'query': 'mutation Write($password: String) { change(value: "request-body-secret") }',
        'operationName': 'operation-name-secret', 'variables': {'password': 'variable-secret'}}
WIRE = b'{"data":{"change":"response-body-secret"}}'


class PrivateFailure(RuntimeError):
    def __str__(self):
        raise AssertionError('exception text must never be formatted')

    def __repr__(self):
        raise AssertionError('exception repr must never be formatted')


def private_failure():
    return PrivateFailure(' '.join(SECRETS))


def events(caplog):
    records = [r for r in caplog.records if hasattr(r, 'diagnostic')]
    for record in records:
        assert record.exc_info is None and record.stack_info is None
        assert json.loads(record.getMessage()) == record.diagnostic
        assert set(record.diagnostic) <= {'event', 'stage', 'exception_type', 'run_id', 'service_id', 'sqlstate'}
        assert record.diagnostic['event'] == 'coverage_internal_failure'
    for secret in SECRETS:
        assert secret not in caplog.text
        assert secret not in repr([r.__dict__ for r in records])
    return [r.diagnostic for r in records]


def wire_headers(run_id):
    return {'X-Coverage-Run-Id': str(run_id), 'Cookie': 'session=cookie-secret',
            'Authorization': 'Bearer token-secret'}


@pytest.mark.parametrize('failure,stage', [
    ('analyzer', 'analyzer'), ('inner_analyzer', 'analyzer'),
    ('document', 'safe_document'), ('document_parse', 'safe_document_parse'),
    ('document_mask', 'safe_document_mask'),
])
async def test_analysis_and_document_failures_are_diagnosed_without_changing_response(
    failure, stage, client, upstream, capture_context, monkeypatch, caplog,
):
    def broken(*args, **kwargs):
        raise private_failure()
    monkeypatch.setattr(proxy, 'capture_e2e_exchange', e2e_capture.capture_e2e_exchange)
    save = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(e2e_capture, 'record_observation', save)
    if failure == 'analyzer':
        capture_context.analyzer.side_effect = private_failure()
    elif failure == 'inner_analyzer':
        monkeypatch.setattr(query_analyzer, '_analyze_query', broken)
        app.state.coverage_analyzer = analyze_e2e_query
    elif failure == 'document':
        monkeypatch.setattr(e2e_capture, 'normalize_document', broken)
    elif failure == 'document_parse':
        monkeypatch.setattr(safe_document, 'parse', broken)
    else:
        monkeypatch.setattr(safe_document, 'print_ast', broken)
    requests = upstream(lambda request: raw_response(202, WIRE))
    result = await client.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
    assert result.status_code == 202 and result.content == WIRE
    assert len(requests) == 1
    observation = save.await_args.args[1]
    assert observation.analysis.status == ('internal_error' if 'analyzer' in failure else 'success')
    assert observation.analysis.fields == ()
    if 'analyzer' not in failure:
        assert observation.safe_document.text is None
    assert events(caplog) == [{'event': 'coverage_internal_failure', 'stage': stage,
        'exception_type': 'PrivateFailure', 'run_id': str(capture_context.run.id)}]


class DriverFailure(Exception):
    sqlstate = '23503'


def storage_failure(state='23503'):
    original = DriverFailure(' '.join(SECRETS))
    original.sqlstate = state
    return DBAPIError('INSERT secret SQL ' + ' '.join(SECRETS), {'password': 'password-secret'}, original)


async def test_observation_save_diagnostic_keeps_502_and_never_retries_mutation(
    client, upstream, capture_context, monkeypatch, caplog,
):
    monkeypatch.setattr(proxy, 'capture_e2e_exchange', e2e_capture.capture_e2e_exchange)
    monkeypatch.setattr(e2e_capture, 'record_observation', AsyncMock(side_effect=storage_failure()))
    requests = upstream(lambda request: raw_response(200, WIRE))
    result = await client.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
    assert result.status_code == 502
    assert result.json() == {'detail': 'Coverage observation could not be saved; upstream may have executed the request. Do not retry automatically.'}
    assert len(requests) == 1
    assert events(caplog) == [{'event': 'coverage_internal_failure', 'stage': 'observation_save',
        'exception_type': 'DBAPIError', 'sqlstate': '23503', 'run_id': str(capture_context.run.id)}]
    assert all(secret not in result.text for secret in SECRETS)


async def test_preflight_diagnostic_preserves_503_and_no_upstream_call(client, upstream, capture_context, caplog):
    capture_context.session.get.side_effect = storage_failure('08006')
    requests = upstream(lambda request: raw_response())
    result = await client.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
    assert result.status_code == 503
    assert result.json() == {'detail': 'Coverage storage is unavailable'}
    assert requests == []
    assert events(caplog) == [{'event': 'coverage_internal_failure', 'stage': 'coverage_preflight',
        'exception_type': 'DBAPIError', 'sqlstate': '08006', 'run_id': str(capture_context.run.id)}]


@pytest.mark.parametrize('error,status', [(httpx.ReadTimeout, 504), (httpx.ConnectError, 502)])
async def test_upstream_diagnostics_omit_url_and_preserve_no_retry(error, status, client, upstream, capture_context, caplog):
    def broken(request):
        raise error(' '.join(SECRETS), request=httpx.Request('POST',
                    'https://private.invalid/graphql?password=query-string-secret'))
    requests = upstream(broken)
    result = await client.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
    assert result.status_code == status and 'Do not retry automatically.' in result.text
    assert len(requests) == 1
    capture_context.capture.assert_not_awaited()
    assert events(caplog) == [{'event': 'coverage_internal_failure', 'stage': 'upstream_request',
        'exception_type': error.__name__, 'run_id': str(capture_context.run.id)}]
    assert 'private.invalid' not in caplog.text


@pytest.mark.parametrize('endpoint,handler,stage,detail', [
    ('/api/schemas/import', 'import_sdl_schema', 'sdl_import_storage',
     {'detail': 'Schema storage is unavailable'}),
    ('/api/schemas/import/introspection', 'import_introspection_schema', 'introspection_import_storage',
     {'detail': 'Schema storage is temporarily unavailable', 'code': 'SCHEMA_STORAGE_UNAVAILABLE'}),
])
async def test_schema_storage_diagnostic_preserves_public_response(
    endpoint, handler, stage, detail, client, monkeypatch, caplog,
):
    monkeypatch.setattr(api, handler, AsyncMock(side_effect=storage_failure()))
    service_id = uuid.uuid4()
    payload = {'serviceId': str(service_id)}
    if endpoint.endswith('/import'):
        payload['sdl'] = 'type Query { requestBodySecret: String }'
    result = await client.post(endpoint, json=payload)
    assert result.status_code == 503 and result.json() == detail
    assert events(caplog) == [{'event': 'coverage_internal_failure', 'stage': stage,
        'exception_type': 'DBAPIError', 'sqlstate': '23503', 'service_id': str(service_id)}]


@pytest.mark.parametrize('state', ['password-secret', '23503\n', '2350', None, 23503])
def test_sqlstate_is_validated_and_arbitrary_identifiers_are_rejected(state, caplog):
    from app.diagnostics import internal_failure
    internal_failure('observation_save', storage_failure(state), run_id='token-secret', service_id='cookie-secret')
    assert events(caplog) == [{'event': 'coverage_internal_failure', 'stage': 'observation_save',
                              'exception_type': 'DBAPIError'}]


def test_invalid_sdl_is_classified_without_catching_builder_programming_errors(monkeypatch):
    with pytest.raises(ValueError, match='Invalid or unsupported GraphQL schema'):
        schema_parser.parse_sdl('type Query { user: UnknownType }')
    error = TypeError('programming-error')
    def broken(*args, **kwargs):
        raise error
    monkeypatch.setattr(schema_parser, 'build_ast_schema', broken)
    with pytest.raises(TypeError) as caught:
        schema_parser.parse_sdl('type Query { user: String }')
    assert caught.value is error


async def test_unrelated_import_typeerror_is_not_converted_to_422(client, monkeypatch):
    monkeypatch.setattr(api, 'import_sdl_schema', AsyncMock(side_effect=TypeError('programming-error')))
    with pytest.raises(TypeError, match='programming-error'):
        await client.post('/api/schemas/import', json={'serviceId': str(uuid.uuid4()), 'sdl': 'type Query { x: String }'})


async def test_cancellation_is_not_diagnosed_as_internal_error(client, upstream, capture_context, monkeypatch, caplog):
    monkeypatch.setattr(proxy, 'capture_e2e_exchange', e2e_capture.capture_e2e_exchange)
    capture_context.analyzer.side_effect = asyncio.CancelledError()
    save = AsyncMock()
    monkeypatch.setattr(e2e_capture, 'record_observation', save)
    requests = upstream(lambda request: raw_response())
    with pytest.raises(asyncio.CancelledError):
        await client.post('/graphql', json=BODY, headers=wire_headers(capture_context.run.id))
    assert len(requests) == 1
    save.assert_not_awaited()
    assert events(caplog) == []
