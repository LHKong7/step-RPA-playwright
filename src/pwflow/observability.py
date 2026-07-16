"""Structured logging: one JSON line per event, with run context attached.

The CLI wants pretty, human-readable log lines (Rich handles that). A service
piped into Loki/CloudWatch/ELK wants the opposite — one JSON object per line, with
`run_id` and `flow` on *every* record so you can filter a single run out of a dozen
concurrent ones. Same log calls, two renderings; `configure_logging(fmt=...)` picks.

Context (`run_id`, `flow`, `step`) is bound with `bind_context(...)` and carried on
a `contextvars.ContextVar`, so it survives `await` boundaries and stays correct when
several runs interleave on one event loop. A `logging.Filter` copies the current
context onto each record; the JSON formatter emits it. Per-event fields ride along
via ``logger.info("msg", extra={"fields": {...}})``.
"""

from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# Standard LogRecord attributes — anything NOT in here that a caller stuffs onto a
# record via `extra=` is treated as a structured field and emitted in the JSON.
_RESERVED = frozenset(
    logging.makeLogRecord({}).__dict__
) | {"message", "asctime", "taskName"}

_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "pwflow_log_context", default={}  # noqa: B039 - never mutated in place; bind() copies
)


def bind(**fields: Any) -> contextvars.Token:
    """Merge fields into the ambient log context. Returns a token for `unbind`."""
    return _context.set({**_context.get(), **fields})


def unbind(token: contextvars.Token) -> None:
    _context.reset(token)


@contextmanager
def bind_context(**fields: Any) -> Iterator[None]:
    """Scope `fields` onto every log record emitted inside the block."""
    token = bind(**fields)
    try:
        yield
    finally:
        unbind(token)


def current_context() -> dict[str, Any]:
    return dict(_context.get())


class ContextFilter(logging.Filter):
    """Stamp the ambient context onto each record so a formatter can emit it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.pwflow_context = _context.get()  # type: ignore[attr-defined]
        return True


class JsonFormatter(logging.Formatter):
    """Render a record as a single JSON line: ts, level, logger, msg, + context + fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        ctx = getattr(record, "pwflow_context", None)
        if ctx:
            payload.update(ctx)

        # Two ways to attach structured fields to one event:
        #   log.info("done", extra={"fields": {"count": 3}})   -> nested under the keys
        #   log.info("done", extra={"count": 3})                -> loose extra attribute
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in ("pwflow_context", "fields"):
                payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: int | str = logging.INFO,
    fmt: str = "console",
    *,
    console: Any = None,
) -> None:
    """Configure the ``pwflow`` logger tree.

    ``fmt``:
      * ``"console"`` — Rich, human-readable (the CLI default).
      * ``"json"``    — one structured JSON line per record (for a log pipeline).

    Idempotent: replaces pwflow's handlers rather than stacking a new one each call,
    so re-invoking (e.g. per CLI command) does not duplicate every line.
    """
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())

    logger = logging.getLogger("pwflow")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler: logging.Handler
    if fmt == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
    else:
        from rich.console import Console
        from rich.logging import RichHandler

        handler = RichHandler(
            console=console or Console(),
            show_path=False,
            show_time=False,
            markup=False,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))

    handler.addFilter(ContextFilter())
    handler.setLevel(level)
    logger.addHandler(handler)
