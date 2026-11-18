import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx
import pytest

from app.services import create_run, create_service, import_sdl
from test_multi_service_postgres import isolated_database

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = b'{"data":{"health":"ok"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
async def runner_backend(isolated_database, tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{upstream.server_port}"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    key = uuid4().hex + uuid4().hex
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "DATABASE_URL": isolated_database.kw["bind"].url.render_as_string(hide_password=False),
        "COVERAGE_API_KEY": key, "COVERAGE_DEV_MODE": "true", "COVERAGE_DEV_UPSTREAM_ORIGINS": origin,
        "COVERAGE_PUBLIC_ORIGIN": "http://localhost:5173", "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    process = None
    try:
        async with isolated_database() as session:
            service = await create_service(session, "runner-" + uuid4().hex, "Runner", endpoint_url=origin + "/graphql")
            snapshot = await import_sdl(session, service.id, "type Query { health: String }")
            run = await create_run(session, snapshot.id, "External runners", {})
        with (tmp_path / "backend.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            async with httpx.AsyncClient(base_url=base, headers={"X-Coverage-Api-Key": key}, trust_env=False, timeout=5) as client:
                deadline = time.monotonic() + 20
                while True:
                    try:
                        if (await client.get("/health")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None or time.monotonic() > deadline:
                        pytest.fail("Isolated backend did not start; inspect backend.log")
                    await asyncio.sleep(.1)
                yield client, str(run.id), base, key
    finally:
        if process is not None:
            if process.poll() is None:
                if os.name == "nt":
                    await asyncio.to_thread(subprocess.run, ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                            capture_output=True, check=False)
                else:
                    process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=10)
        await asyncio.to_thread(upstream.shutdown)
        upstream.server_close()
        thread.join(timeout=5)


PYTEST_SOURCE = '''import os
import httpx
import pytest

def health(execution):
    response = httpx.post(os.environ['COVERAGE_URL'] + '/graphql',
        headers={**execution.headers, 'X-Coverage-Api-Key': os.environ['COVERAGE_API_KEY']},
        json={'query': '{ health }'}, trust_env=False)
    assert response.status_code == 200
    return response.json()['data']['health']

def test_pass(coverage_test):
    assert health(coverage_test) == 'ok'

def test_assertion_failure(coverage_test):
    assert health(coverage_test) == 'different'

@pytest.fixture
def cleanup():
    yield
    raise AssertionError('Intentional teardown failure')

def test_teardown_failure(coverage_test, cleanup):
    assert health(coverage_test) == 'ok'

@pytest.mark.skip(reason='Intentional skip')
def test_skipped():
    pass
'''

PLAYWRIGHT_SOURCE = '''const {test: base, expect} = require(PACKAGE);
const {createCoverageTest} = require(SDK);
const test = createCoverageTest(base);
async function health(request, coverage) {
  const execution = await coverage.trackRun(process.env.COVERAGE_TEST_RUN_ID);
  const response = await request.post(process.env.COVERAGE_URL + '/graphql', {
    headers: {...execution.headers, 'X-Coverage-Api-Key': process.env.COVERAGE_API_KEY},
    data: {query: '{ health }'},
  });
  expect(response.status()).toBe(200);
  return (await response.json()).data.health;
}
test('passes', async ({request, coverage}) => { expect(await health(request, coverage)).toBe('ok'); });
test('assertion fails', async ({request, coverage}) => { expect(await health(request, coverage)).toBe('different'); });
test('retry succeeds', async ({request, coverage}, info) => { expect(await health(request, coverage)).toBe(info.retry ? 'ok' : 'different'); });
const cleanup = test.extend({broken: [async ({}, use) => { await use(); throw new Error('Intentional teardown failure'); }, {auto: true}]});
cleanup('teardown fails', async ({request, coverage}) => { expect(await health(request, coverage)).toBe('ok'); });
test.skip('skipped', async () => {});
'''


@pytest.mark.parametrize("framework", ["pytest", "playwright"])
async def test_external_runner_persists_real_results_and_fields(runner_backend, tmp_path, framework):
    client, run_id, base, key = runner_backend
    env = {**os.environ, "COVERAGE_TEST_RUN_ID": run_id, "COVERAGE_URL": base, "COVERAGE_API_KEY": key,
           "PYTHONPATH": str(ROOT / "integrations/python/src"), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTEST_ADDOPTS", None)
    if framework == "pytest":
        (tmp_path / "test_external.py").write_text(PYTEST_SOURCE, encoding="utf-8")
        (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        command = [sys.executable, "-m", "pytest", "-q", "-c", "pytest.ini", "-p", "coverage_test.pytest_plugin", "test_external.py"]
    else:
        package = ROOT / "integrations/playwright/node_modules/@playwright/test"
        assert package.is_dir(), "Run npm ci --prefix integrations/playwright"
        sdk = ROOT / "integrations/playwright/index.cjs"
        source = PLAYWRIGHT_SOURCE.replace("PACKAGE", json.dumps(str(package))).replace("SDK", json.dumps(str(sdk)))
        (tmp_path / "external.spec.cjs").write_text(source, encoding="utf-8")
        (tmp_path / "playwright.config.cjs").write_text("module.exports=" + json.dumps({
            "testDir": str(tmp_path), "workers": 2, "fullyParallel": True, "retries": 1,
            "reporter": [["list"], [str(sdk)]], "outputDir": str(tmp_path / "results")}), encoding="utf-8")
        command = ["node", str(package / "cli.js"), "test", "--config", str(tmp_path / "playwright.config.cjs")]
    result = await asyncio.to_thread(subprocess.run, command, cwd=tmp_path, env=env, capture_output=True,
                                    text=True, encoding="utf-8", timeout=90)
    (tmp_path / "runner.log").write_text((result.stdout + result.stderr).replace(key, "<redacted>"), encoding="utf-8")
    assert result.returncode == 1, "Expected intentional child failures; inspect runner.log"
    response = await client.get(f"/api/runs/{run_id}/tests", params={"limit": 500})
    assert response.status_code == 200
    rows = response.json()
    assert {row["framework"] for row in rows} == {framework}
    assert len({row["id"] for row in rows}) == len(rows)
    assert Counter(row["status"] for row in rows) == (
        Counter(passed=1, failed=2, skipped=1) if framework == "pytest" else Counter(passed=2, failed=5, skipped=1))
    for row in rows:
        fields = (await client.get(f"/api/runs/{run_id}/tests/{row['id']}/fields")).json()
        if row["status"] == "skipped":
            assert row["requestCount"] == 0 and fields == []
        else:
            assert row["requestCount"] == row["countedRequestCount"] == row["coveredFieldCount"] == 1
            assert [(field["typeName"], field["fieldName"], field["hits"], field["observedCount"]) for field in fields] == [("Query", "health", 1, 1)]
    if framework == "pytest":
        cleanup = next(row for row in rows if row["testId"].endswith("test_teardown_failure"))
        assert cleanup["failureCode"] == "teardown_failed"
    else:
        retried = [row for row in rows if "retry succeeds" in row["name"]]
        assert {(row["attempt"], row["status"]) for row in retried} == {(1, "failed"), (2, "passed")}
        assert len({row["testId"] for row in retried}) == 1
