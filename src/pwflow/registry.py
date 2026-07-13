"""The action registry.

Each action bundles a pydantic params model with its implementation, which is what
lets the loader reject ``clcik:`` or a ``fill:`` with no ``value:`` *before* a browser
is ever launched.

Two flavours:

* **leaf actions** (``click``, ``extract``, ...) — the engine deep-renders their
  payload, then validates it. By the time the implementation runs, every ``{{ }}``
  is gone and the params are real Python values.
* **control actions** (``foreach``, ``if``, ...) — declared with ``control=True``.
  They own nested steps whose templates must be re-evaluated on every iteration, so
  the engine hands them the *unrendered* payload and they render their own condition
  fields themselves.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .errors import FlowLoadError
from .models import Step

P = TypeVar("P", bound=BaseModel)
Impl = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ActionSpec:
    name: str  # canonical name
    model: type[BaseModel]
    impl: Impl
    shorthand: str | None  # field a scalar payload collapses into
    control: bool
    doc: str
    aliases: tuple[str, ...] = ()

    def normalize(self, raw: Any) -> dict[str, Any]:
        """Expand shorthand payloads into the full mapping form.

        A scalar collapses into the shorthand field (``click: ".btn"``). A *mapping*
        does too, but only when none of its keys names a parameter of this action —
        that is what lets a structured selector be the shorthand::

            click: {role: button, name: "Buy"}   -> {selector: {role: ..., name: ...}}
            click: {selector: ".btn", force: true}  -> taken as the params it plainly is
        """
        if raw is None:
            return {}
        if self.shorthand is None:
            if not isinstance(raw, dict):
                raise FlowLoadError(
                    f"action `{self.name}` takes a mapping, got {type(raw).__name__}: {raw!r}"
                )
            return raw
        if isinstance(raw, dict):
            keys = set(raw)
            fields = set(self.model.model_fields) | {
                f.alias for f in self.model.model_fields.values() if f.alias
            }
            return raw if keys & fields else {self.shorthand: raw}
        return {self.shorthand: raw}

    def validate(self, raw: Any, *, where: str) -> BaseModel:
        try:
            return self.model.model_validate(self.normalize(raw))
        except ValidationError as e:
            hint = ""
            if self.shorthand and isinstance(raw, dict):
                hint = (
                    f"\n  (a mapping with any of {sorted(self.model.model_fields)} is read as "
                    f"`{self.name}`'s parameters, not as a `{self.shorthand}`)"
                )
            raise FlowLoadError(f"{where}: invalid `{self.name}`\n{_pretty(e)}{hint}") from e


REGISTRY: dict[str, ActionSpec] = {}


def action(
    name: str,
    model: type[BaseModel],
    *,
    shorthand: str | None = None,
    aliases: tuple[str, ...] = (),
    control: bool = False,
) -> Callable[[Impl], Impl]:
    """Register an action under ``name`` (and any aliases)."""

    def decorate(impl: Impl) -> Impl:
        spec = ActionSpec(
            name=name,
            model=model,
            impl=impl,
            shorthand=shorthand,
            control=control,
            doc=(impl.__doc__ or "").strip().splitlines()[0] if impl.__doc__ else "",
            aliases=aliases,
        )
        for key in (name, *aliases):
            if key in REGISTRY:
                raise RuntimeError(f"action `{key}` is already registered")
            REGISTRY[key] = spec
        return impl

    return decorate


def get(name: str) -> ActionSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY))
        raise FlowLoadError(f"unknown action `{name}`. Known: {known}") from None


def _pretty(e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def canonical() -> list[ActionSpec]:
    """Every action once, under its canonical name (aliases folded in)."""
    by_name = {spec.name: spec for spec in REGISTRY.values()}
    return [by_name[name] for name in sorted(by_name)]


def nested_steps(params: BaseModel) -> list[Step]:
    """Every ``Step`` reachable from a control action's params, for recursive binding."""
    found: list[Step] = []
    for value in params.__dict__.values():
        if isinstance(value, Step):
            found.append(value)
        elif isinstance(value, list):
            found.extend(v for v in value if isinstance(v, Step))
    return found
