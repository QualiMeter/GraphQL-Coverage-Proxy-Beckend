"""Allowlisted error events: never format exceptions, requests or SQL values."""

import json
import logging
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy.exc import DBAPIError

logger = logging.getLogger(__name__)
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
    # Stage is a code-owned constant at each call site. No exception message,
    # traceback/locals, operation name, URL, query hash or arbitrary context.
    event = {"event": "coverage_internal_failure", "stage": stage,
             "exception_type": type(exc).__name__}
    for name, value in (("run_id", run_id if run_id is not None else _run_id.get()),
                        ("service_id", service_id)):
        if isinstance(value, uuid.UUID):
            event[name] = str(value)
    if isinstance(exc, DBAPIError):
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
            event["sqlstate"] = sqlstate
    # JSON in the message remains useful with the default Uvicorn logging setup;
    # the structured attribute supports handlers that consume LogRecord fields.
    logger.error(json.dumps(event, sort_keys=True), extra={"diagnostic": event})
