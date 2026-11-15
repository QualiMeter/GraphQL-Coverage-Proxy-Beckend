"""Allowlisted error events: never format exceptions, requests or SQL values."""

import json
import logging
import queue
import re
import sys
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy.exc import DBAPIError


class _SafeStreamHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            self.stream = sys.stderr
            super().emit(record)
        except Exception:
            pass  # No fallback output, including when emit itself fails.

    def handleError(self, record):
        # StreamHandler catches formatter/write/flush errors internally. Its
        # default handleError can print a chained exception even without exc_info.
        pass


logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)
logger.propagate = False  # Never reach an unprotected parent/last-resort path.
logger.addHandler(_SafeStreamHandler())
_pending = queue.Queue(maxsize=128)


def _write_events():
    while True:
        message, event = _pending.get()
        try:
            logger.error(message, extra={"diagnostic": event})
        except Exception:
            pass  # A broken handler/filter must not kill the consumer or retry.
        finally:
            _pending.task_done()


# One daemon per application process. Only sanitized data crosses this boundary;
# neither the original exception nor its handled-exception context is retained.
threading.Thread(target=_write_events, name="coverage-diagnostics", daemon=True).start()
_run_id = ContextVar("diagnostic_run_id", default=None)


@contextmanager
def diagnostic_run(run_id: uuid.UUID):
    # Context propagates through asyncio.to_thread; reset also on cancellation.
    token = _run_id.set(run_id)
    try:
        yield
    finally:
        _run_id.reset(token)


def internal_failure(stage: str, exc: Exception, *, run_id: uuid.UUID | None = None,
                     service_id: uuid.UUID | None = None):
    # Metadata and enqueueing are best effort. Never format/retain the exception
    # or perform handler I/O in the request task (including its active except).
    try:
        event = {"event": "coverage_internal_failure", "stage": stage,
                 "exception_type": type(exc).__name__}
        for name, value in (("run_id", run_id if run_id is not None else _run_id.get()),
                            ("service_id", service_id)):
            if isinstance(value, uuid.UUID):
                event[name] = str(value)
        if isinstance(exc, DBAPIError):
            try:
                sqlstate = getattr(exc.orig, "sqlstate", None)
                if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
                    event["sqlstate"] = sqlstate
            except Exception:
                pass  # Keep the event without optional driver metadata.
        _pending.put_nowait((json.dumps(event, sort_keys=True), event))
    except Exception:
        # Includes metadata/serialization failures and queue.Full. Do not fall
        # back to another logger or stderr. BaseException/cancellation propagates.
        pass
