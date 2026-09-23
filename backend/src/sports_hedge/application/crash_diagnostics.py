"""Process-crash diagnostics for the paper API.

Fatal interpreter faults go to stderr via faulthandler. Unhandled asyncio
exceptions and an exception escaping FastAPI lifespan log the #519 event-loop
activity snapshot. The Windows demo launcher redirects that stderr stream to
logs\\demo-backend.err.log. This module does not dump tracebacks on a timer.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import sys
from collections.abc import Callable
from typing import Any

from sports_hedge.application.event_loop_activity import loop_activity_diagnostic_snapshot
from sports_hedge.application.serving_build import get_serving_build_info

LOGGER = logging.getLogger(__name__)

AsyncioExceptionHandler = Callable[[asyncio.AbstractEventLoop, dict[str, Any]], object]


def enable_faulthandler() -> None:
    """Write fatal interpreter and native-fault tracebacks to process stderr.

    Idempotent. Does not call faulthandler.dump_traceback_later.
    """

    faulthandler.enable(file=sys.stderr, all_threads=True)


def install_asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop | None = None,
) -> AsyncioExceptionHandler | None:
    """Log unhandled async failures, then chain to the previous or default handler."""

    running = loop if loop is not None else asyncio.get_running_loop()
    previous = running.get_exception_handler()

    def _handler(active: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        try:
            _log_unhandled_async(context)
        finally:
            if previous is not None:
                previous(active, context)
            else:
                active.default_exception_handler(context)

    running.set_exception_handler(_handler)
    return previous


def record_backend_fatal(exc: BaseException) -> None:
    """Emit one backend_fatal line. Never raises; the caller re-raises exc."""

    sha: str | None = None
    branch: str | None = None
    try:
        info = get_serving_build_info()
        sha = info.git_sha
        branch = info.git_branch
    except Exception:
        sha = None
        branch = None
    try:
        snapshot: dict[str, object] = dict(loop_activity_diagnostic_snapshot())
    except Exception:
        snapshot = {}
    fields: dict[str, object] = {
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
        "git_sha": sha,
        "git_branch": branch,
    }
    fields.update(snapshot)
    try:
        _log_event("backend_fatal", fields)
    except Exception:
        LOGGER.error(
            "backend_fatal exception_type=%s exception_message=%s",
            type(exc).__name__,
            exc,
        )


def _log_unhandled_async(context: dict[str, Any]) -> None:
    exception = context.get("exception")
    if isinstance(exception, BaseException):
        exception_type: str | None = type(exception).__name__
        exception_message: object = str(exception)
    else:
        exception_type = None
        exception_message = None
    try:
        snapshot: dict[str, object] = dict(loop_activity_diagnostic_snapshot())
    except Exception:
        snapshot = {}
    fields: dict[str, object] = {
        "message": context.get("message"),
        "exception_type": exception_type,
        "exception_message": exception_message,
        "task": _task_identity(context.get("task")),
        "future": _task_identity(context.get("future")),
    }
    fields.update(snapshot)
    _log_event("unhandled_async_exception", fields)


def _task_identity(value: object) -> str | None:
    if value is None:
        return None
    name_fn = getattr(value, "get_name", None)
    label: str | None = None
    if callable(name_fn):
        try:
            label = str(name_fn())
        except Exception:
            label = None
    text = repr(value)
    if label:
        return f"{label} {text}"
    return text


def _log_event(event: str, fields: dict[str, object]) -> None:
    rendered = " ".join(f"{key}={_render(value)}" for key, value in fields.items())
    LOGGER.error("%s %s", event, rendered)


def _render(value: object) -> str:
    if value is None:
        return "None"
    text = str(value).replace("\r", " ").replace("\n", " ")
    if text == "" or any(char.isspace() for char in text):
        return repr(text)
    return text
