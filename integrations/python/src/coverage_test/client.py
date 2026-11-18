from contextvars import ContextVar
from datetime import datetime, timezone
import time
from uuid import uuid4

import httpx

CURRENT_TEST = ContextVar("coverage_test", default=None)


class CoverageTrackingError(RuntimeError):
    pass


class TestExecution:
    def __init__(self, base_url, run_id, api_key, test_id, name, *, framework="pytest", attempt=1,
                 execution_id=None, team_key=None, suite_key=None):
        url = httpx.URL(base_url)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("Use a Coverage API base URL without credentials, query or fragment")
        self.base = str(url).rstrip("/")
        self.run_id, self.key = str(run_id), api_key
        self.id = str(execution_id or uuid4())
        self.payload = {"id": self.id, "testId": test_id, "name": " ".join(name.split())[:512], "framework": framework, "attempt": attempt}
        if team_key is not None:
            self.payload["teamKey"] = team_key
        if suite_key is not None:
            self.payload["suiteKey"] = suite_key
        self.started = self.finished = False
        self.snapshot = None
        self.final_result = None

    @property
    def path(self):
        return f"/api/runs/{self.run_id}/tests/{self.id}"

    @property
    def headers(self):
        return {"X-Coverage-Run-Id": self.run_id, "X-Coverage-Test-Execution-Id": self.id}

    def _request(self, method, path, payload=None):
        deadline = time.monotonic() + 5 if path.endswith("/finish") else None
        for attempt in range(3):
            remaining = deadline - time.monotonic() if deadline is not None else 10
            if remaining <= 0:
                raise CoverageTrackingError("Coverage metadata request timed out")
            try:
                with httpx.Client(timeout=min(10, remaining), trust_env=False, follow_redirects=False) as client:
                    response = client.request(method, self.base + path, json=payload,
                                              headers={"X-Coverage-Api-Key": self.key})
            except httpx.HTTPError:
                if attempt == 2:
                    raise CoverageTrackingError("Coverage metadata request could not be completed") from None
                time.sleep(.1 * (attempt + 1))
                continue
            if response.status_code >= 500 and attempt < 2:
                time.sleep(.1 * (attempt + 1))
                continue
            if not response.is_success:
                try:
                    data = response.json() if response.status_code == 409 else {}
                except ValueError:
                    data = {}
                busy = data.get("detail", {}) if isinstance(data, dict) else {}
                if isinstance(busy, dict) and busy.get("code") == "test_requests_in_flight":
                    if (path.endswith("/finish") and attempt < 2
                            and time.monotonic() + .1 * (attempt + 1) < deadline):
                        time.sleep(.1 * (attempt + 1))
                        continue
                    raise CoverageTrackingError("Test still has requests in flight")
                raise CoverageTrackingError(f"Coverage metadata request returned HTTP {response.status_code}")
            try:
                response.json()
            except ValueError:
                raise CoverageTrackingError("Coverage metadata response is not JSON") from None
            return response
        raise CoverageTrackingError("Coverage metadata request failed")

    def start(self):
        response = self._request("POST", f"/api/runs/{self.run_id}/tests", self.payload).json()
        self._identity(response)
        self.started = True
        return self

    def finish(self, status, duration_ms, failure_code=None):
        if not self.started:
            self.start()
        payload = {"status": status, "durationMs": duration_ms, "failureCode": failure_code}
        if self.final_result is None:
            self.final_result = payload
        elif payload != self.final_result:
            raise CoverageTrackingError("Cannot replace the known test result")
        response = self._request("POST", self.path + "/finish", payload).json()
        self._identity(response)
        if response.get("status") != status:
            raise CoverageTrackingError("Coverage API did not confirm the submitted result")
        self.finished = True
        return self.read()

    def _identity(self, data):
        if not isinstance(data, dict) or data.get("id") != self.id or data.get("runId") != self.run_id:
            raise CoverageTrackingError("Coverage API returned a different test execution")

    def _pages(self, path):
        rows, offset = [], 0
        separator = "&" if "?" in path else "?"
        while True:
            response = self._request("GET", f"{path}{separator}limit=500&offset={offset}")
            batch = response.json()
            if not isinstance(batch, list):
                raise CoverageTrackingError("Coverage API did not return a page")
            rows.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(response.headers.get("X-Total-Count", offset)):
                return rows

    def read(self):
        data = self._request("GET", self.path).json()
        self._identity(data)
        self.snapshot = {"source": "backend", "fetchedAt": datetime.now(timezone.utc).isoformat(),
                         "execution": data,
                         "fields": self._pages(self.path + "/fields"),
                         "requests": self._pages(f"/api/runs/{self.run_id}/requests?testExecutionId={self.id}")}
        return self.snapshot


def begin_current_test(base_url, run_id, api_key):
    context = CURRENT_TEST.get()
    if context is None:
        return None
    key = (base_url.rstrip("/"), str(run_id))
    if key not in context.executions:
        execution = TestExecution(
            base_url, run_id, api_key, context.test_id, context.name, attempt=context.attempt,
            team_key=getattr(context, "team_key", None), suite_key=getattr(context, "suite_key", None),
        )
        context.executions[key] = execution
        execution.start()
    return context.executions[key]
