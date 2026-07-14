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

import logging
import re
from typing import Any, Literal
from urllib.parse import urljoin

from playwright.async_api import Locator, Page
from pydantic import model_validator

from ..errors import ActionError
from ..registry import action
from ._common import RunContext, Selector, Step, Strict, loc, tmo

log = logging.getLogger("pwflow")

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
        records = await _list_records(ctx, root, p)
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


# --------------------------------------------------------------------------
# List extraction: a single-evaluate fast path over the per-field locator path.
#
# `extract` with `fields` costs one driver round-trip *per field per record* — 30 rows ×
# 3 fields ≈ 90 round-trips. When every selector is plain CSS, one page.evaluate reads the
# whole table in a single trip, and Python applies the exact same `_post` (cast/regex/trim/
# absurl) to the raw strings — so the result is byte-for-byte identical to the locator path.
#
# Eligibility is safe by construction: a bare-string selector is what Playwright feeds to
# its CSS engine anyway, so querySelectorAll is equivalent. Anything else (a SelectorSpec,
# an xpath `//...`, a `text=`/`role=` engine, a `>>` chain) is not CSS — and if the guess is
# ever wrong, querySelectorAll throws and we fall back to the locator path transparently.
# --------------------------------------------------------------------------

_ENGINE_PREFIX = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*=")

_LIST_JS = """
(args) => {
  const { listSel, fields, limit } = args;
  let rows = Array.from(document.querySelectorAll(listSel));
  if (limit != null) rows = rows.slice(0, limit);
  const read = (el, f) => {
    switch (f.type) {
      case 'text': return el.textContent;
      case 'html': return el.outerHTML;
      case 'inner_html': return el.innerHTML;
      case 'value': return el.value ?? null;
      case 'attr': return el.getAttribute(f.attr);
      case 'link': return el.getAttribute('href');
      default: return null;
    }
  };
  return rows.map(row => {
    const rec = {};
    for (const f of fields) {
      if (f.many) {
        const els = f.sel ? Array.from(row.querySelectorAll(f.sel)) : [row];
        rec[f.key] = els.map(el => read(el, f));
      } else {
        const el = f.sel ? row.querySelector(f.sel) : row;
        rec[f.key] = el ? read(el, f) : null;
      }
    }
    return rec;
  });
}
"""


def _css(selector: Any) -> str | None:
    """Return a CSS string iff `selector` is a bare string Playwright treats as CSS."""
    if not isinstance(selector, str):
        return None  # a SelectorSpec (role/label/within/...) is never plain CSS
    s = selector.strip()
    if not s or s.startswith(("//", "..", "xpath=")) or ">>" in s:
        return None  # xpath, or a Playwright selector chain
    if _ENGINE_PREFIX.match(s):
        return None  # an explicit engine: text= / role= / id= / ...
    return s


def _field_specs(p: Extract) -> list[tuple[str | None, FieldSpec]]:
    """(name, spec) per output field. name is None for a single-value (no `fields`) list."""
    if p.fields is None:
        single = FieldSpec(
            type=p.type, attr=p.attr, regex=p.regex, cast=p.cast, trim=p.trim, default=p.default
        )
        return [(None, single)]
    return list(p.fields.items())


def _fast_eligible(specs: list[tuple[str | None, FieldSpec]]) -> bool:
    for _, f in specs:
        if f.type in ("count", "exists"):
            return False  # a per-record count/exists is not worth compiling; use locators
        if f.selector is not None and _css(f.selector) is None:
            return False
    return True


async def _list_records(ctx: RunContext, root: Locator, p: Extract) -> list[Any]:
    css = _css(p.selector)
    specs = _field_specs(p)
    if css is not None and _fast_eligible(specs):
        try:
            return await _list_fast(ctx, css, specs, p)
        except Exception as e:  # noqa: BLE001 - identical result via the locator path
            log.debug("extract '%s': fast path fell back to locators (%s)", p.name, e)

    items = await root.all()
    if p.limit is not None:
        items = items[: p.limit]
    return [await _record(ctx, item, p) for item in items]


async def _list_fast(
    ctx: RunContext, css: str, specs: list[tuple[str | None, FieldSpec]], p: Extract
) -> list[Any]:
    descriptors = [
        {
            "key": name or "__value__",
            "sel": None if f.selector is None else _css(f.selector),
            "type": f.type,
            "attr": f.attr,
            "many": f.many,
        }
        for name, f in specs
    ]
    raw_records: list[dict] = await ctx.page.evaluate(
        _LIST_JS, {"listSel": css, "fields": descriptors, "limit": p.limit}
    )

    out: list[Any] = []
    if p.fields is None:
        _, spec = specs[0]
        out = [_post_raw(ctx, rec["__value__"], spec) for rec in raw_records]
    else:
        for rec in raw_records:
            out.append({name: _post_raw(ctx, rec[name], f) for name, f in specs})
    return out


def _post_raw(ctx: RunContext, raw: Any, f: FieldSpec) -> Any:
    """Apply the same post-processing the locator path uses to a JS-returned raw value."""
    if f.many:
        return [v for v in (_post(ctx, x, f) for x in (raw or [])) if v is not None]
    return _post(ctx, raw, f)
