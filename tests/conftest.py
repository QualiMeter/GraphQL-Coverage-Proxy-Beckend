"""Capture the isolated async diagnostic path without weakening privacy assertions."""
import pytest

from app import diagnostics


def drain_diagnostics():
    # Test-only bounded wait: inspect records only after real background emission.
    # No production flush/join or request-path waiting is introduced.
    pending = getattr(diagnostics, '_pending', None)
    if pending is not None:
        with pending.all_tasks_done:
            assert pending.all_tasks_done.wait_for(lambda: pending.unfinished_tasks == 0, timeout=5)


class _DiagnosticCapture:
    def __init__(self, capture):
        self.capture = capture

    def __getattr__(self, name):
        if name in {'text', 'records', 'record_tuples', 'get_records'}:
            drain_diagnostics()
        return getattr(self.capture, name)


@pytest.fixture
def caplog(caplog):
    # The production logger deliberately does not propagate to root handlers.
    # Attach pytest's normal capture explicitly; retain every original assertion.
    isolated = not diagnostics.logger.propagate
    if isolated:
        diagnostics.logger.addHandler(caplog.handler)
    try:
        yield _DiagnosticCapture(caplog)
    finally:
        drain_diagnostics()
        if isolated:
            diagnostics.logger.removeHandler(caplog.handler)
