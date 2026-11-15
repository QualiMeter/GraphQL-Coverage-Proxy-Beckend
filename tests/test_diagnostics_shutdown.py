"""Assert interpreter exit, including after the real application lifespan stops."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = r'''
import asyncio
from contextlib import asynccontextmanager
import sys
import threading
import uuid

from sqlalchemy.exc import DBAPIError
fault, mode = sys.argv[1:]
entered = threading.Event()
never_release = threading.Event()


def block_sink():
    from app import diagnostics
    handler = next(h for h in diagnostics.logger.handlers if hasattr(h, 'stream'))
    original_stderr = sys.stderr
    original_stream = handler.stream

    class StuckStderr:
        def write(self, text):
            if fault == 'write':
                entered.set()
                never_release.wait()
            return original_stream.write(text)

        def flush(self):
            if fault == 'flush':
                entered.set()
                never_release.wait()
            return original_stream.flush()

    if fault != 'none':
        # On the old HEAD emit selects sys.stderr; the fixed sink owns its stream.
        # Restore process stderr before exit: only diagnostic I/O remains stuck.
        handler.stream = sys.stderr = StuckStderr()
    try:
        try:
            raise DBAPIError('INSERT private-sql', {'password': 'private-parameter'},
                             RuntimeError('private-exception-token'))
        except Exception as exc:
            diagnostics.internal_failure('observation_save', exc, run_id=uuid.UUID(int=1))
        if fault == 'none':
            diagnostics._pending.join()
        else:
            assert entered.wait(3), 'consumer never entered the broken sink'
            print('SINK_BLOCKED', flush=True)
    finally:
        sys.stderr = original_stderr


async def run_server():
    import httpx
    import uvicorn
    from app.server import server_config

    # Production constructs Config before loading app.main and its handlers.
    config = server_config()
    from app.main import app
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def observed_lifespan(application):
        async with original_lifespan(application):
            yield
        print('ASGI_LIFESPAN_STOPPED', flush=True)

    app.router.lifespan_context = observed_lifespan
    config.host, config.port = '127.0.0.1', 0
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError('Uvicorn stopped before startup')
                await asyncio.sleep(0.01)
        block_sink()
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f'http://127.0.0.1:{port}/health', timeout=3)
            assert response.status_code == 200 and response.json() == {'status': 'ok'}
        print('HTTP_SERVED', flush=True)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
    assert all(not listener.is_serving() for listener in server.servers)
    print('UVICORN_STOPPED', flush=True)


if mode == 'uvicorn':
    asyncio.run(run_server())
else:
    block_sink()
print('MAIN_RETURNED', flush=True)
'''


@pytest.mark.parametrize('mode', ['python', 'uvicorn'])
@pytest.mark.parametrize('fault', ['none', 'write', 'flush'])
def test_diagnostic_sink_cannot_hold_interpreter_shutdown(mode, fault):
    process = subprocess.Popen(
        [sys.executable, '-c', PROBE, fault, mode], cwd=ROOT,
        env={**os.environ, 'FORWARDED_ALLOW_IPS': '127.0.0.1'},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=12)
    except subprocess.TimeoutExpired:
        # A red test must reap the hung interpreter, including its daemon thread.
        process.kill()
        stdout, stderr = process.communicate(timeout=3)
        assert 'MAIN_RETURNED' in stdout, (stdout, stderr)
        if mode == 'uvicorn':
            assert 'ASGI_LIFESPAN_STOPPED' in stdout and 'UVICORN_STOPPED' in stdout
        pytest.fail(f'{mode}/{fault}: interpreter hung after main/lifespan returned')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
    assert process.returncode == 0, (stdout, stderr)
    assert 'MAIN_RETURNED' in stdout
    if fault != 'none':
        assert 'SINK_BLOCKED' in stdout
    if mode == 'uvicorn':
        assert 'HTTP_SERVED' in stdout
        assert 'ASGI_LIFESPAN_STOPPED' in stdout
        assert 'UVICORN_STOPPED' in stdout
    if fault == 'write':
        assert '"event": "coverage_internal_failure"' not in stderr
    else:
        assert '"event": "coverage_internal_failure"' in stderr
    for private in ('private-sql', 'private-parameter', 'private-exception-token', 'Logging error'):
        assert private not in stdout + stderr
