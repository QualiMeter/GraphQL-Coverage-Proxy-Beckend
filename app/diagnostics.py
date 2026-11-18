"""Allowlisted error events: never format exceptions, requests or SQL values."""

import json
import logging
import queue
import re
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy.exc import DBAPIError


class _SafeStreamHandler(logging.StreamHandler):
    # Only the single diagnostic consumer performs I/O. No handler lock or sink
    # flush may make logging.shutdown wait for that daemon's blocked write/flush.
    def createLock(self):
        self.lock = None

    def __init__(self):
        super().__init__()
        try:
            # Own the Python buffer, borrow fd 2. A blocked diagnostic write must
            # not hold sys.stderr's buffer lock during interpreter finalization.
            self.stream = open(2, "w", buffering=1, encoding="utf-8",
                               errors="backslashreplace", closefd=False)
        except OSError:
            self.stream = None  # No usable stderr; drop events without fallback.

    def flush(self):
        # logging.shutdown calls this on the main thread. Consumer flushes below.
        pass

    def emit(self, record):
        try:
            stream = self.stream
            if stream is not None:
                stream.write(self.format(record) + self.terminator)
                stream.flush()
        except Exception:
            pass  # No fallback output, including when emit itself fails.

    def handleError(self, record):
        # Never use the default traceback fallback, even if called explicitly.
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


def outbound_event(stage: str, outcome: str, *, method=None, elapsed_ms=None,
                   status=None, attempt=None, family=None):
    """Only fixed labels and numeric metadata; never accept exception/request data."""
    try:
        if stage not in {"dns", "policy", "request", "tcp", "tls", "send_headers", "send_body",
                         "response_headers", "response_body", "http", "complete", "reconcile"}:
            return
        if outcome not in {"error", "timeout", "rejected", "invalid", "ok", "fallback_connected",
                           "matched", "not_found"}:
            return
        event = {"event": "coverage_outbound", "stage": stage, "outcome": outcome}
        if method in {"GET", "POST"}:
            event["method"] = method
        for key, value in (("elapsed_ms", elapsed_ms), ("status", status),
                           ("attempt", attempt), ("family", family)):
            if type(value) is int and 0 <= value < 2**63:
                event[key] = value
        run_id = _run_id.get()
        if isinstance(run_id, uuid.UUID):
            event["run_id"] = str(run_id)
        _pending.put_nowait((json.dumps(event, sort_keys=True), event))
    except Exception:
        pass
