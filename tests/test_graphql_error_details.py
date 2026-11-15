"""Upstream metadata is untrusted even when it looks like a GraphQL name."""
import asyncio
import json
import threading
from unittest.mock import AsyncMock

import httpx
import pytest

from app import e2e_capture, graphql_errors as errors, proxy
from app.api_models import GraphQLErrorDetailOut, RequestDetailOut, RequestOut
from app.graphql_errors import ErrorSummary, response_envelope, sanitize_errors
from test_proxy_e2e import capture_context  # noqa: F401

SDL = 'type Query { users: [User!]! health: String } type User { id: ID name: String child: User }'
QUERY = '{ users { id name } health }'
CANARIES = ('secret_password', 'secret_api_key', 'secret_cookie', 'secret_sql',
            'secret_variables', 'secret_exception_message', 'https://internal.invalid/?secret=private')


def clean(payload, query=QUERY, sdl=SDL, operation=None):
    return sanitize_errors(payload, query, operation, sdl)


@pytest.mark.parametrize('payload,count', [
    ({'data': {}}, 0), ({}, 0), ({'errors': []}, 0),
    ({'errors': [{}]}, 1), ({'data': {'users': []}, 'errors': [{}, {}]}, 2),
    ({'errors': None}, None), ({'errors': {}}, None), ({'errors': 'private'}, None),
    ({'errors': [None]}, None), ({'errors': [{}, 'private']}, None),
])
def test_envelope_classification(payload, count):
    summary = clean(payload)
    assert summary.count == count
    assert summary.availability == ('unavailable' if count is None else 'sanitized')
    if count is None:
        assert summary.details is None
    elif count == 0:
        assert summary.details == []


@pytest.mark.parametrize('body', [None, b'not json', b'[]', b'null', b'\xff', b'[' * 2000,
                                  b' ' * (errors.MAX_RESPONSE_BYTES + 1)])
def test_unreadable_response_is_unknown(body):
    payload, data, has_errors = response_envelope(body)
    assert (payload, data, has_errors) == (None, None, None)
    assert clean(payload) == ErrorSummary()


@pytest.mark.parametrize('encoding', ['br', 'gzip, deflate', 'gzip', 'deflate'])
def test_unsupported_or_corrupt_encoding_is_unknown(encoding):
    decoded = proxy.decoded_response_copy(b'not compressed', httpx.Headers({'Content-Encoding': encoding}))
    assert clean(response_envelope(decoded)[0]) == ErrorSummary()


def test_only_allowlisted_codes_and_server_messages_survive(caplog, capfd):
    payload = {'errors': [{'message': secret, 'path': [secret], 'extensions': {
        'code': secret, 'nested': {'stacktrace': [secret]}, 'exception': secret},
        'locations': [{'line': secret, 'column': secret, 'private': secret}]} for secret in CANARIES]}
    payload['errors'].append({'message': ' '.join(CANARIES),
        'extensions': {'code': 'GRAPHQL_VALIDATION_FAILED', 'private': CANARIES},
        'path': ['users', 0, 'id'], 'locations': [{'line': 3, 'column': 5}]})
    result = clean(payload)
    serialized = json.dumps(result.details)
    for secret in CANARIES:
        assert secret not in serialized + caplog.text + repr([r.__dict__ for r in caplog.records]) + str(capfd.readouterr())
    assert result.details[0] == {'code': None, 'path': None, 'locations': [], 'safeMessage': None}
    assert result.details[-1] == {'code': 'GRAPHQL_VALIDATION_FAILED', 'path': ['users', 0, 'id'],
        'locations': [{'line': 3, 'column': 5}], 'safeMessage': errors.SAFE_MESSAGES['GRAPHQL_VALIDATION_FAILED']}
    assert GraphQLErrorDetailOut(**result.details[-1]).model_dump(by_alias=True) == result.details[-1]


@pytest.mark.parametrize('query,path,expected', [
    (QUERY, ['users', 0, 'id'], ['users', 0, 'id']),
    (QUERY, ['users'], ['users']),
    (QUERY, ['health'], ['health']),
    (QUERY, ['id'], None), (QUERY, ['health', 'id'], None),
    (QUERY, ['users', 'id'], None), (QUERY, ['users', 0, 'child'], None),
    (QUERY, ['users', True, 'id'], None), (QUERY, ['users', -1, 'id'], None),
    (QUERY, ['users', 1_000_001, 'id'], None),
    (QUERY, ['users', 1_000_000, 'id'], ['users', 1_000_000, 'id']),
    ('{ health: users { id } }', ['health', 0, 'id'], None),
    ('{ users: users { id } }', ['users', 0, 'id'], None),
    ('{ users { name: id } }', ['users', 0, 'name'], None),
    ('{ users { ... on User { id } } }', ['users', 0, 'id'], None),
    ('{ users { ...U } } fragment U on User { id }', ['users', 0, 'id'], None),
    ('{ users { id } users { name } }', ['users', 0, 'id'], None),
    ('query A { health } query B { users { id } }', ['health'], None),
    (None, ['users'], None), ('{ broken', ['users'], None),
    (QUERY, [], None), (QUERY, [0], None), (QUERY, 'users', None),
    (QUERY, ['x' * 65], None), (QUERY, ['users'] * 17, None),
])
def test_paths_require_unaliased_selection_in_snapshot(query, path, expected):
    assert clean({'errors': [{'path': path}]}, query=query).details[0]['path'] == expected


def test_selected_operation_and_snapshot_are_respected():
    query = 'query A { health } query B { users { id } }'
    assert clean({'errors': [{'path': ['health']}]}, query, operation='A').details[0]['path'] == ['health']
    assert clean({'errors': [{'path': ['health']}]}, query, operation='B').details[0]['path'] is None
    assert clean({'errors': [{'path': ['health']}]}, sdl='type Query { other: String }').details[0]['path'] is None


@pytest.mark.parametrize('value', [-1, 0, True, False, 1.5, '1', None, 1_000_001, {}, []])
def test_locations_discard_invalid_coordinates(value):
    result = clean({'errors': [{'locations': [{'line': value, 'column': 1}, {'line': 1, 'column': value},
        {'line': 1_000_000, 'column': 1}, 'private', None]}]})
    assert result.details[0]['locations'] == [{'line': 1_000_000, 'column': 1}]


def test_limits_keep_exact_count_and_bound_serialized_json():
    result = clean({'errors': [{}] * 33})
    assert result.count == 33 and len(result.details) == 20 and result.availability == 'truncated'
    result = clean({'errors': [{'locations': [{'line': 1, 'column': 1}] * 9}]})
    assert len(result.details[0]['locations']) == 8 and result.availability == 'truncated'
    name = 'a' * 64
    sdl = f'type Query {{ {name}: User }} type User {{ {name}: User }}'
    query = '{' + (name + '{') * 15 + name + '}' * 16
    path = [name] * 16
    result = clean({'errors': [{'path': path}] * 20}, query, sdl)
    assert result.count == 20 and result.availability == 'truncated'
    assert 0 < len(result.details) < 20
    assert len(json.dumps(result.details, ensure_ascii=True).encode()) <= errors.MAX_DETAILS_BYTES


def test_new_metadata_is_detail_only():
    fields = {'graphql_error_count', 'graphql_error_details', 'graphql_error_details_availability'}
    assert fields <= RequestDetailOut.model_fields.keys()
    assert fields.isdisjoint(RequestOut.model_fields)


@pytest.mark.parametrize("stage", ["sanitize_errors", "error_count"])
async def test_optional_sanitizer_failure_preserves_flags_and_count(stage, capture_context, monkeypatch, caplog, capfd):
    ctx = capture_context
    def broken(*args):
        raise RuntimeError(' '.join(CANARIES))
    monkeypatch.setattr(e2e_capture, stage, broken)
    save = AsyncMock()
    monkeypatch.setattr(e2e_capture, 'record_observation', save)
    await e2e_capture.capture_e2e_exchange(ctx.session, run_id=ctx.run.id,
        request_body=b'{"query":"{ ok }"}', decoded_response_body=b'{"data":{},"errors":[{}]}',
        http_status=200, duration_ms=1, analyzer=ctx.analyzer)
    observation = save.await_args.args[1]
    assert observation.graphql_has_errors is True and observation.response_has_data is True
    assert observation.graphql_errors == ErrorSummary(count=1 if stage == "sanitize_errors" else None)
    assert not caplog.records and not capfd.readouterr().err


async def test_sanitizing_runs_off_event_loop_and_cancellation_propagates(capture_context, monkeypatch):
    ctx = capture_context
    entered, release = threading.Event(), threading.Event()
    def slow(*args):
        entered.set()
        assert release.wait(5)
        return ErrorSummary(0, 'sanitized', [])
    monkeypatch.setattr(e2e_capture, 'sanitize_errors', slow)
    save = AsyncMock()
    monkeypatch.setattr(e2e_capture, 'record_observation', save)
    task = asyncio.create_task(e2e_capture.capture_e2e_exchange(ctx.session, run_id=ctx.run.id,
        request_body=b'{"query":"{ ok }"}', decoded_response_body=b'{"data":{}}',
        http_status=200, duration_ms=1, analyzer=ctx.analyzer))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():
                await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        save.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
