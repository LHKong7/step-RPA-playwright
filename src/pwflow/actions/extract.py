"""Pulling data off the page — the reason this project exists.

The shape people actually want, most of the time, is *a list of records*::

    - extract:
        name: stories
        selector: ".athing"          # one match = one record
        list: true
        append: true                 # accumulate across pagination
        fields:
          title: ".titleline > a"                      # string shorthand = text
          url:   { selector: ".titleline > a", type: link }   # absolute href
          rank:  { selector: ".rank", cast: int }

Field selectors are scoped to their record, so ``.titleline > a`` means "inside this
row", not "anywhere on the page". Results land in ``data.<name>`` and are visible to
every later step as ``{{ data.stories }}``.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urljoin

from playwright.async_api import Locator, Page
from pydantic import model_validator

from ..errors import ActionError
from ..registry import action
from ._common import RunContext, Selector, Step, Strict, loc, tmo

ValueType = Literal[
    "text",  # visible text, whitespace-collapsed
    "html",  # outerHTML
    "inner_html",
    "attr",  # needs `attr:`
    "link",  # href, resolved against the page URL
    "value",  # form control value
    "count",  # number of matches
    "exists",  # bool
]
Cast = Literal["int", "float", "str", "bool"]


class FieldSpec(Strict):
    selector: Selector | None = None  # relative to the record; omit to use the record itself
    type: ValueType = "text"
    attr: str | None = None
    many: bool = False  # collect every match instead of the first
    regex: str | None = None  # keep group 1 of this pattern
    cast: Cast | None = None
    trim: bool = True
    default: Any = None

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, v: Any) -> Any:
        # `title: ".titleline > a"` means "the text of that element"
        return {"selector": v} if isinstance(v, str) else v

    @model_validator(mode="after")
    def _attr_needed(self) -> FieldSpec:
        if self.type == "attr" and not self.attr:
            raise ValueError("`type: attr` needs an `attr:` name")
        return self


class Extract(Strict):
    name: str  # key under `data`
    selector: Selector | None = None
    list: bool = False  # one record per match
    append: bool = False  # extend `data.<name>` instead of replacing it
    fields: dict[str, FieldSpec] | None = None
    # used when `fields` is absent — extract a single value instead of a record
    type: ValueType = "text"
    attr: str | None = None
    regex: str | None = None
    cast: Cast | None = None
    trim: bool = True
    default: Any = None
    limit: int | None = None  # cap the number of records
    skip_empty: bool = True  # drop records whose every field came back empty


@action("extract", Extract)
async def extract(ctx: RunContext, p: Extract, step: Step) -> Any:
    """Scrape values or records into ``data.<name>``."""
    timeout = tmo(ctx, step)
    root: Page | Locator = ctx.page if p.selector is None else loc(ctx, p.selector)

    # `count` / `exists` are questions about the match set itself.
    if p.fields is None and p.type in ("count", "exists"):
        n = 0 if isinstance(root, Page) else await root.count()
        value = n if p.type == "count" else n > 0
        ctx.put_data(p.name, value, append=p.append)
        return value

    if p.list:
        if isinstance(root, Page):
            raise ActionError("`list: true` needs a `selector:` to iterate over")
        # Wait for the first match so we do not race an empty page, but let an
        # intentionally-empty result through instead of blowing up.
        try:
            await root.first.wait_for(state="attached", timeout=timeout)
        except Exception:  # noqa: BLE001 - genuinely empty page is a valid outcome
            pass
        items = await root.all()
        if p.limit is not None:
            items = items[: p.limit]
        records = [await _record(ctx, item, p) for item in items]
        if p.skip_empty:
            records = [r for r in records if not _is_empty(r)]
        ctx.put_data(p.name, records, append=p.append)
        return records

    if isinstance(root, Locator):
        root = root.first
        await root.wait_for(state="attached", timeout=timeout)
    value = await _record(ctx, root, p)
    ctx.put_data(p.name, value, append=p.append)
    return value


async def _record(ctx: RunContext, scope: Page | Locator, p: Extract) -> Any:
    """One record: a dict when `fields` is given, otherwise a single value."""
    if p.fields is None:
        single = FieldSpec(
            type=p.type, attr=p.attr, regex=p.regex, cast=p.cast, trim=p.trim, default=p.default
        )
        return await _field(ctx, scope, single)
    return {name: await _field(ctx, scope, spec) for name, spec in p.fields.items()}


async def _field(ctx: RunContext, scope: Page | Locator, f: FieldSpec) -> Any:
    target: Page | Locator = scope if f.selector is None else loc(ctx, f.selector, scope)

    if f.type == "count":
        return await target.count() if isinstance(target, Locator) else 0
    if f.type == "exists":
        return (await target.count()) > 0 if isinstance(target, Locator) else False

    if isinstance(target, Page):
        raw_values = [await target.content()] if f.type in ("html", "inner_html") else [None]
    elif f.many:
        raw_values = [await _read(el, f) for el in await target.all()]
    else:
        raw_values = [await _read(target.first, f)] if await target.count() else []

    values = [_post(ctx, v, f) for v in raw_values]
    if f.many:
        return [v for v in values if v is not None]
    return values[0] if values else f.default


async def _read(el: Locator, f: FieldSpec) -> Any:
    if f.type == "text":
        return await el.text_content()
    if f.type == "html":
        return await el.evaluate("e => e.outerHTML")
    if f.type == "inner_html":
        return await el.inner_html()
    if f.type == "value":
        return await el.input_value()
    if f.type == "attr":
        return await el.get_attribute(f.attr)  # type: ignore[arg-type]
    if f.type == "link":
        return await el.get_attribute("href")
    raise ActionError(f"extract: unsupported type `{f.type}`")  # pragma: no cover


def _post(ctx: RunContext, value: Any, f: FieldSpec) -> Any:
    if value is None:
        return f.default
    if isinstance(value, str):
        if f.trim:
            value = " ".join(value.split())
        if f.type == "link":
            value = urljoin(ctx.page.url, value)
        if f.regex:
            import re

            m = re.search(f.regex, value)
            if not m:
                return f.default
            value = m.group(1) if m.groups() else m.group(0)
        if value == "" and f.default is not None:
            return f.default
    if f.cast:
        value = _cast(value, f.cast, f.default)
    return value


def _cast(value: Any, cast: Cast, default: Any) -> Any:
    from ..template import _to_float, _to_int

    try:
        if cast == "int":
            out = _to_int(value, None)
            return default if out is None else out
        if cast == "float":
            out = _to_float(value, None)
            return default if out is None else out
        if cast == "bool":
            return str(value).strip().lower() not in ("", "false", "0", "no", "none")
        return str(value)
    except (TypeError, ValueError):
        return default


def _is_empty(record: Any) -> bool:
    if isinstance(record, dict):
        return all(v in (None, "", [], {}) for v in record.values())
    return record in (None, "", [], {})
