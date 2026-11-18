import asyncio
import threading
import uuid
from unittest.mock import AsyncMock

import pytest
from graphql import GraphQLError

from app import services
from app.api_models import SchemaImport
from app.schema_parser import parse_sdl


def large_sdl(count=30_000):
    return 'type Query { ' + ' '.join(f'f{i}: String' for i in range(count)) + ' }'


async def test_sdl_import_runs_parser_off_loop_and_services_other_coroutines(monkeypatch):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    started, release = asyncio.Event(), threading.Event()
    original = services.parse_sdl
    parsed_fields = []

    def parser(sdl):
        assert threading.get_ident() != loop_thread, 'SDL parser blocked the event loop'
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10), 'other coroutine could not run'
        parsed = original(sdl)
        parsed_fields.append(len(parsed.fields))
        return parsed

    monkeypatch.setattr(services, 'parse_sdl', parser)
    saved = AsyncMock(return_value='saved')
    monkeypatch.setattr(services, '_save_parsed_schema', saved)
    session = AsyncMock()
    session.get.return_value = object()
    task = asyncio.create_task(services.import_sdl(session, uuid.uuid4(), large_sdl()))
    started_wait = asyncio.create_task(started.wait())
    try:
        done, _ = await asyncio.wait([task, started_wait],
                                     timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert started.is_set()
        assert not saved.called
        release.set()
        assert await task == 'saved'
        assert parsed_fields == [30_000]
    finally:
        release.set()
        started_wait.cancel()
        await asyncio.gather(task, started_wait, return_exceptions=True)


@pytest.mark.parametrize('kind', ['schema', 'comparison'])
async def test_cancelled_running_work_retains_capacity_until_thread_finishes(kind, monkeypatch):
    from app.schema_work import MAX_SCHEMA_WORKERS, MAX_COMPARISON_WORKERS, schema_work, comparison_work
    submissions = []
    if kind == 'comparison':
        from app.schema_work import _comparison_executor
        submit = _comparison_executor.submit
        def counted_submit(*args, **kwargs):
            submissions.append(1)
            return submit(*args, **kwargs)
        monkeypatch.setattr(_comparison_executor, 'submit', counted_submit)
        schema_work, MAX_SCHEMA_WORKERS = comparison_work, MAX_COMPARISON_WORKERS
    loop = asyncio.get_running_loop()
    entered, released = [], threading.Event()
    full = asyncio.Event()
    active = peak = 0
    mutex = threading.Lock()

    def work(index):
        nonlocal active, peak
        with mutex:
            active += 1
            peak = max(peak, active)
            entered.append(index)
            if len(entered) == MAX_SCHEMA_WORKERS:
                loop.call_soon_threadsafe(full.set)
        try:
            assert released.wait(10)
        finally:
            with mutex:
                active -= 1
        return index

    tasks = [asyncio.create_task(schema_work(work, i)) for i in range(12)]
    try:
        await asyncio.wait_for(full.wait(), 10)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        more = [asyncio.create_task(schema_work(work, i)) for i in range(12, 16)]
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(entered) == MAX_SCHEMA_WORKERS
        if kind == 'comparison':
            # Dedicated executor's thread limit alone could hide early slot
            # release by queuing extra futures. No new submission is allowed.
            assert len(submissions) == MAX_SCHEMA_WORKERS
        released.set()
        assert await asyncio.gather(*more) == list(range(12, 16))
        assert peak <= MAX_SCHEMA_WORKERS
    finally:
        released.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize('sdl,error', [
    ('type Query {', GraphQLError),
    ('type Query { x: Missing }', (TypeError, ValueError)),
    (large_sdl(50_001), ValueError),
    ('type Query { x: String } ' + ' '.join(f'type T{i} {{ x: String }}' for i in range(5000)), ValueError),
])
async def test_offloaded_parser_preserves_complexity_limits_and_errors(sdl, error):
    from app.schema_work import schema_work
    with pytest.raises(error):
        await schema_work(parse_sdl, sdl)


def test_sdl_size_limit_unchanged():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SchemaImport(service_id=uuid.uuid4(), sdl='x' * 1_000_001)


@pytest.mark.parametrize('kind', ['schema', 'comparison'])
async def test_worker_preserves_context_and_releases_capacity_after_error(kind):
    from contextvars import ContextVar
    from app.schema_work import schema_work, comparison_work
    worker = comparison_work if kind == 'comparison' else schema_work
    marker = ContextVar('worker-test-marker', default='missing')
    token = marker.set('request-context')
    def check(fail=False):
        assert marker.get() == 'request-context'
        if kind == 'comparison':
            assert threading.current_thread().name.startswith('coverage-comparison')
        if fail:
            raise ValueError('worker failure')
        return 42
    try:
        with pytest.raises(ValueError, match='worker failure'):
            await worker(check, True)
        assert await worker(check) == 42
    finally:
        marker.reset(token)
