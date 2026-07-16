"""Structured logging: JSON rendering and context binding."""

import json
import logging

from pwflow.observability import (
    JsonFormatter,
    bind,
    bind_context,
    configure_logging,
    current_context,
    unbind,
)


def _record(msg: str, level: int = logging.INFO, **extra) -> logging.LogRecord:
    rec = logging.LogRecord("pwflow", level, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_core_fields():
    line = JsonFormatter().format(_record("hello"))
    obj = json.loads(line)
    assert obj["level"] == "INFO"
    assert obj["logger"] == "pwflow"
    assert obj["msg"] == "hello"
    assert "ts" in obj


def test_json_formatter_includes_bound_context():
    with bind_context(run_id="r1", flow="demo"):
        rec = _record("x", pwflow_context=current_context())
    obj = json.loads(JsonFormatter().format(rec))
    assert obj["run_id"] == "r1"
    assert obj["flow"] == "demo"


def test_json_formatter_nested_fields_and_loose_extra():
    rec = _record("done", fields={"count": 3}, step="2.1")
    obj = json.loads(JsonFormatter().format(rec))
    assert obj["count"] == 3
    assert obj["step"] == "2.1"


def test_json_formatter_captures_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = _record("failed", level=logging.ERROR, exc_info=sys.exc_info())
    obj = json.loads(JsonFormatter().format(rec))
    assert "ValueError: boom" in obj["exc"]


def test_bind_context_is_scoped():
    assert current_context() == {}
    with bind_context(a=1):
        assert current_context()["a"] == 1
        with bind_context(b=2):
            assert current_context() == {"a": 1, "b": 2}
        assert "b" not in current_context()
    assert current_context() == {}


def test_bind_unbind_tokens():
    token = bind(k="v")
    assert current_context()["k"] == "v"
    unbind(token)
    assert "k" not in current_context()


def test_configure_logging_json_emits_one_line_per_record(capsys):
    configure_logging(fmt="json")
    log = logging.getLogger("pwflow")
    with bind_context(run_id="abc"):
        log.info("event one", extra={"fields": {"n": 1}})
    err = capsys.readouterr().err.strip().splitlines()
    obj = json.loads(err[-1])
    assert obj["msg"] == "event one"
    assert obj["run_id"] == "abc"
    assert obj["n"] == 1


def test_configure_logging_is_idempotent():
    configure_logging(fmt="json")
    configure_logging(fmt="json")
    log = logging.getLogger("pwflow")
    # re-configuring replaces handlers rather than stacking them
    assert len(log.handlers) == 1


def test_configure_logging_console_uses_rich():
    configure_logging(fmt="console")
    log = logging.getLogger("pwflow")
    assert len(log.handlers) == 1
    assert type(log.handlers[0]).__name__ == "RichHandler"
    # leave the tree in json mode so other tests that inspect output are unaffected
    configure_logging(fmt="json")
