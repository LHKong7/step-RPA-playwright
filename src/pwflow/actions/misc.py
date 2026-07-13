"""Variables, JavaScript, artifacts, and writing results out."""

from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Any, Literal

from ..errors import ActionError
from ..registry import action
from ._common import RunContext, Selector, Step, Strict, loc, tmo

log = logging.getLogger("pwflow")


class SetVars(Strict):
    model_config = Strict.model_config | {"extra": "allow"}  # the keys *are* the variable names


@action("set", SetVars)
async def set_vars(ctx: RunContext, p: SetVars, step: Step) -> dict[str, Any]:
    """Assign flow variables: ``set: {page_no: "{{ page_no + 1 }}"}``."""
    values = p.model_dump()  # already rendered by the engine
    ctx.vars.update(values)
    return values


class Log(Strict):
    message: Any
    level: Literal["debug", "info", "warning", "error"] = "info"


@action("log", Log, shorthand="message", aliases=("print", "echo"))
async def log_(ctx: RunContext, p: Log, step: Step) -> None:
    """Print a message — the print-debugging of flows."""
    getattr(log, p.level)("%s", p.message)


class Evaluate(Strict):
    script: str
    arg: Any = None
    selector: Selector | None = None  # run against an element instead of the page
    name: str | None = None  # also store the result under `data.<name>`


@action("evaluate", Evaluate, shorthand="script", aliases=("eval", "js"))
async def evaluate(ctx: RunContext, p: Evaluate, step: Step) -> Any:
    """Run JavaScript in the page and keep its return value.

    The escape hatch, and the fast path: one `evaluate` that returns 200 records beats
    200 round-trips through `extract`'s per-field locators.
    """
    if p.selector is not None:
        result = await loc(ctx, p.selector).evaluate(p.script, p.arg, timeout=tmo(ctx, step))
    else:
        result = await ctx.page.evaluate(p.script, p.arg)
    if p.name:
        ctx.put_data(p.name, result)
    return result


class Screenshot(Strict):
    path: str | None = None  # relative to the run's artifacts dir unless absolute
    full_page: bool = True
    selector: Selector | None = None


@action("screenshot", Screenshot, shorthand="path")
async def screenshot(ctx: RunContext, p: Screenshot, step: Step) -> str:
    """Save a PNG into the run's artifacts directory."""
    name = p.path or f"{ctx.current_index or 'shot'}.png"
    path = Path(name) if Path(name).is_absolute() else ctx.artifact_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if p.selector is not None:
        await loc(ctx, p.selector).screenshot(path=str(path), timeout=tmo(ctx, step))
    else:
        await ctx.page.screenshot(path=str(path), full_page=p.full_page)
    return str(path)


class Save(Strict):
    path: str
    data: Any = None  # defaults to the whole `data` dict
    format: Literal["json", "jsonl", "csv"] = "json"


@action("save", Save)
async def save(ctx: RunContext, p: Save, step: Step) -> str:
    """Write collected data to disk mid-flow (the flow's `output:` block does this at the end)."""
    payload = ctx.data if p.data is None else p.data
    path = Path(p.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize(payload, p.format), encoding="utf-8")
    ctx.artifacts.append(str(path))
    log.info("wrote %s", path)
    return str(path)


def serialize(payload: Any, fmt: Literal["json", "jsonl", "csv"]) -> str:
    if fmt == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    rows = _rows(payload)
    if fmt == "jsonl":
        return "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)

    if not rows:
        return ""
    columns: list[str] = []
    for row in rows:
        for k in row:
            if k not in columns:
                columns.append(k)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(_flatten(r) for r in rows)
    return buf.getvalue()


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    """CSV cells are scalars. A list/dict cell becomes JSON, not a Python repr."""
    return {
        k: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else v
        for k, v in row.items()
    }


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Coerce whatever `data` holds into a list of records for jsonl/csv."""
    if isinstance(payload, list):
        return [r if isinstance(r, dict) else {"value": r} for r in payload]
    if isinstance(payload, dict):
        # A dict whose only list value is the obvious table: `{stories: [...]}`.
        lists = [v for v in payload.values() if isinstance(v, list)]
        if len(lists) == 1:
            return _rows(lists[0])
        return [payload]
    raise ActionError(f"cannot serialize {type(payload).__name__} as rows")
