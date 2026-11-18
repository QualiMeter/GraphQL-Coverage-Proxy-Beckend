import asyncio
import uuid

import pytest

from test_test_coverage_postgres import context, exchange, start, BODY
from test_multi_service_postgres import isolated_database, routed_client
from test_graphql_proxy import upstream, raw_response

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('tagged', [False, True])
async def test_run_finish_waits_for_admitted_exchange(context, upstream, tagged):
    client, sessions, run_id, _ = context
    execution = (await start(client, run_id))[0]['id'] if tagged else None
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(request):
        entered.set()
        await release.wait()
        return raw_response(body=BODY)

    upstream(slow)
    task = asyncio.create_task(exchange(client, run_id, execution))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert sessions.kw['bind'].pool.checkedout() == 0
        result = await client.post(f'/api/runs/{run_id}/finish')
        assert result.status_code == 409, result.text
    finally:
        release.set()
        response = await task
    assert response.status_code == 200, response.text
    assert (await client.get(f'/api/runs/{run_id}/requests')).headers['X-Total-Count'] == '1'
    assert (await client.post(f'/api/runs/{run_id}/finish')).status_code == 200



@pytest.mark.parametrize('cancelled', [False, True])
async def test_untagged_failed_or_cancelled_exchange_releases_run(context, upstream, cancelled):
    import httpx
    client, sessions, run_id, _ = context
    entered = asyncio.Event()
    async def slow(request):
        entered.set()
        if cancelled:
            await asyncio.Event().wait()
        raise httpx.ReadTimeout('response lost; do not replay mutation')
    calls = upstream(slow)
    task = asyncio.create_task(exchange(client, run_id))
    await asyncio.wait_for(entered.wait(), 3)
    if cancelled:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task).status_code == 504
    assert len(calls) == 1
    assert (await client.post(f'/api/runs/{run_id}/finish')).status_code == 200


async def test_cancellation_after_admission_commit_still_releases_run(context, monkeypatch):
    from app import proxy
    client, sessions, run_id, calls = context
    committed, release = asyncio.Event(), asyncio.Event()
    original = proxy.admit_request
    async def delayed(*args):
        await original(*args)
        committed.set()
        await release.wait()
    monkeypatch.setattr(proxy, 'admit_request', delayed)
    task = asyncio.create_task(exchange(client, run_id))
    await asyncio.wait_for(committed.wait(), 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not calls
    assert sessions.kw['bind'].pool.checkedout() == 0
    assert (await client.post(f'/api/runs/{run_id}/finish')).status_code == 200
