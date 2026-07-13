"""``{{ ... }}`` template rendering.

Two behaviours, both borrowed from the way Ansible / GitHub Actions treat templates,
because flow authors already expect them:

* A string that is *entirely* one expression returns a **native Python value**::

      times: "{{ vars.pages }}"      -> 2      (int, not "2")
      in:    "{{ data.stories }}"    -> [...]  (list, not its repr)

* Anything else renders to a string::

      path: "out/{{ flow.name }}-{{ now() }}.json"   -> "out/hn-2026-07-14T10:00:00.json"

Expressions run in a sandboxed Jinja2 environment, so a flow cannot reach into
Python internals via ``__class__`` and friends.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from jinja2 import StrictUndefined, Undefined
from jinja2.exceptions import TemplateError as JinjaTemplateError
from jinja2.sandbox import SandboxedEnvironment

from .errors import TemplateError

# A string made of exactly one expression and nothing else.
_SOLE_EXPR = re.compile(r"^\s*\{\{(?P<expr>.*)\}\}\s*$", re.DOTALL)


def _regex(value: Any, pattern: str, group: int | str = 1, default: Any = None) -> Any:
    """`"Rank 12" | regex('(\\d+)')` -> "12". The bread-and-butter scraping filter."""
    m = re.search(pattern, "" if value is None else str(value))
    if not m:
        return default
    try:
        return m.group(group)
    except IndexError:
        return default


def _regex_all(value: Any, pattern: str) -> list[str]:
    return re.findall(pattern, "" if value is None else str(value))


def _to_int(value: Any, default: int | None = None) -> int | None:
    """Tolerant int: pulls the first integer out of things like "1,234 points"."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    m = re.search(r"-?\d[\d,]*", "" if value is None else str(value))
    if not m:
        return default
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return default


def _to_float(value: Any, default: float | None = None) -> float | None:
    m = re.search(r"-?\d[\d,]*\.?\d*", "" if value is None else str(value))
    if not m:
        return default
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return default


def _absurl(value: Any, base: str) -> str | None:
    """Turn a scraped ``href="/item?id=1"`` into an absolute URL."""
    if not value:
        return None
    return urljoin(base, str(value))


def _unique(seq: Any) -> list:
    seen, out = set(), []
    for x in seq or []:
        k = json.dumps(x, sort_keys=True, default=str) if isinstance(x, (dict, list)) else x
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


class FlowEnvironment(SandboxedEnvironment):
    """Jinja2, with one correction that matters a great deal here.

    Jinja resolves ``a.b`` by trying ``getattr(a, "b")`` *first* and only then ``a["b"]``.
    On a dict that means ``data.items`` hands you ``dict.items`` — the bound method —
    rather than the records you just scraped into ``data["items"]``. Same trap for
    ``keys``, ``values``, ``get``, ``count``, ``copy``, ``pop``.

    In a data DSL the key is always what the author meant, so keys win. Real methods
    stay reachable when no key shadows them.
    """

    def getattr(self, obj: Any, attribute: str) -> Any:
        if isinstance(obj, dict) and attribute in obj:
            return obj[attribute]
        return super().getattr(obj, attribute)


def build_env() -> SandboxedEnvironment:
    env = FlowEnvironment(undefined=StrictUndefined)
    env.filters.update(
        regex=_regex,
        regex_all=_regex_all,
        to_int=_to_int,
        to_float=_to_float,
        absurl=_absurl,
        unique=_unique,
        strip=lambda v: str(v).strip() if v is not None else None,
    )
    env.globals.update(
        now=lambda: datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        json=json.dumps,
    )
    return env


ENV = build_env()


def render(value: Any, context: dict[str, Any]) -> Any:
    """Render one value. Non-strings pass through untouched."""
    if not isinstance(value, str) or "{{" not in value:
        return value

    sole = _SOLE_EXPR.match(value)
    try:
        if sole:
            expr = sole.group("expr")
            # Guard against "{{a}} {{b}}", which the greedy match would mangle.
            if "{{" not in expr and "}}" not in expr:
                result = ENV.compile_expression(expr.strip(), undefined_to_none=False)(**context)
                if isinstance(result, Undefined):
                    # StrictUndefined only raises when *used*; an expression that merely
                    # returns it would otherwise smuggle a ghost value into the params.
                    result._fail_with_undefined_error()
                return result
        return ENV.from_string(value).render(**context)
    except JinjaTemplateError as e:
        raise TemplateError(f"{value!r}: {e}") from e
    except Exception as e:  # noqa: BLE001 - user expressions can raise anything
        raise TemplateError(f"{value!r}: {type(e).__name__}: {e}") from e


def render_deep(value: Any, context: dict[str, Any]) -> Any:
    """Render every string inside a nested dict/list payload."""
    if isinstance(value, dict):
        return {k: render_deep(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_deep(v, context) for v in value]
    return render(value, context)


def truthy(expr: str, context: dict[str, Any]) -> bool:
    """Evaluate a condition. Accepts ``"x > 1"`` and ``"{{ x > 1 }}"`` alike."""
    stripped = expr.strip()
    if not (stripped.startswith("{{") and stripped.endswith("}}")):
        stripped = "{{ " + stripped + " }}"
    result = render(stripped, context)
    if isinstance(result, str):
        # A string-rendered condition ("false", "", "0") should not read as true.
        return result.strip().lower() not in ("", "false", "none", "no", "0")
    return bool(result)
