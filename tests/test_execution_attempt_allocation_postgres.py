import asyncio
import uuid

import pytest
from test_test_coverage_postgres import context
from test_multi_service_postgres import isolated_database, routed_client
from test_graphql_proxy import upstream

pytestmark = pytest.mark.integration

async def test_parallel_automatic_attempts_and_lost_response(context):
    client, _, run_id, _ = context
    payload = dict(testId='same-test', name='same', framework='pytest', attempt=None)
    ids = [str(uuid.uuid4()) for _ in range(8)]
    responses = await asyncio.gather(*(client.post(f'/api/runs/{run_id}/tests', json={**payload, 'id': ident}) for ident in ids))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert sorted(r.json()['attempt'] for r in responses) == list(range(1, 9))
    # Discarded response after commit: replay UUID must recover its allocated attempt.
    again = await client.post(f'/api/runs/{run_id}/tests', json={**payload, 'id': ids[0]})
    assert again.json() == responses[0].json()
    result = dict(status='passed', durationMs=17)
    path = f'/api/runs/{run_id}/tests/{ids[0]}/finish'
    committed = await client.post(path, json=result)
    assert (await client.post(path, json=result)).json() == committed.json()
    assert (await client.post(path, json={**result, 'status': 'failed'})).status_code == 409
    conflict = await client.post(f'/api/runs/{run_id}/tests', json={**payload, 'id': str(uuid.uuid4()), 'attempt': 2})
    assert conflict.status_code == 409

