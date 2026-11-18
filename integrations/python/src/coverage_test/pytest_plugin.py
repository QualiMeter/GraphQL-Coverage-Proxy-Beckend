import os
from types import SimpleNamespace

import pytest

from coverage_test.client import CURRENT_TEST, CoverageTrackingError, begin_current_test


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    marker = item.get_closest_marker("case")
    name = str(marker.args[0]) if marker and marker.args else item.name
    context = SimpleNamespace(test_id=item.nodeid, name=name[:512], attempt=1, executions={}, reports=[])
    token = CURRENT_TEST.set(context)
    contexts = getattr(item.config, "_coverage_test_contexts", [])
    contexts.append(context)
    item.config._coverage_test_contexts = contexts
    try:
        yield
    finally:
        CURRENT_TEST.reset(token)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    context = CURRENT_TEST.get()
    if context is None:
        return
    attempt = max(1, int(getattr(item, "execution_count", 1)))
    if context.attempt != attempt:
        context.attempt, context.executions, context.reports = attempt, {}, []
    run_id = os.environ.get("COVERAGE_TEST_RUN_ID")
    if run_id:
        base, key = os.environ.get("COVERAGE_URL"), os.environ.get("COVERAGE_API_KEY")
        if not base or not key:
            raise CoverageTrackingError("COVERAGE_TEST_RUN_ID requires COVERAGE_URL and COVERAGE_API_KEY")
        begin_current_test(base, run_id, key)


def publish(context, status, failure_code):
    duration_ms = round(sum(report.duration for report in context.reports) * 1000)
    for execution in context.executions.values():
        if execution.finished:
            continue
        execution.finish(status, duration_ms, failure_code)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    context = CURRENT_TEST.get()
    if context is None:
        return
    context.reports.append(report)
    if report.when != "teardown":
        return
    failed = next((r for r in context.reports if r.failed or r.outcome == "rerun"), None)
    skipped = any(r.skipped for r in context.reports)
    status = "failed" if failed else "skipped" if skipped else "passed"
    code = {"setup": "setup_failed", "call": "assertion_failed", "teardown": "teardown_failed"}.get(failed.when) if failed else None
    try:
        if not context.executions and os.environ.get("COVERAGE_TEST_RUN_ID"):
            begin_current_test(os.environ.get("COVERAGE_URL"), os.environ.get("COVERAGE_TEST_RUN_ID"),
                               os.environ.get("COVERAGE_API_KEY"))
        publish(context, status, code)
    except (CoverageTrackingError, ValueError) as exc:
        message = f"Test Coverage result was not confirmed: {exc}"
        report.outcome, report.longrepr = "failed", message


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    for context in getattr(session.config, "_coverage_test_contexts", []):
        if any(not execution.finished for execution in context.executions.values()):
            try:
                publish(context, "interrupted", "runner_interrupted")
            except (CoverageTrackingError, ValueError):
                session.exitstatus = 1 if not exitstatus else exitstatus


@pytest.fixture
def coverage_test():
    context = CURRENT_TEST.get()
    if context is None or not context.executions:
        pytest.fail("Set COVERAGE_TEST_RUN_ID, COVERAGE_URL and COVERAGE_API_KEY before using coverage_test")
    return next(iter(context.executions.values()))
