"""YAML -> validated ``Flow``.

Loading is where authoring mistakes should die. After ``load_flow`` returns, every
step is known to name a real action and to carry parameters of the right shape,
including steps nested inside ``foreach`` / ``if`` / ``try`` bodies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from . import actions  # noqa: F401  -- import for the registration side effect
from .errors import FlowLoadError
from .models import Flow, Step
from .registry import get as get_action
from .registry import nested_steps


def load_flow(source: str | Path, *, name_hint: str | None = None) -> Flow:
    """Load a flow from a path or from raw YAML text."""
    if isinstance(source, Path) or (
        isinstance(source, str) and "\n" not in source and source.endswith((".yaml", ".yml"))
    ):
        path = Path(source)
        if not path.exists():
            raise FlowLoadError(f"no such flow file: {path}")
        text, name_hint = path.read_text(), name_hint or path.stem
    else:
        text = str(source)

    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise FlowLoadError(f"YAML is malformed: {e}") from e

    if not isinstance(payload, dict):
        raise FlowLoadError("a flow must be a mapping with at least `name:` and `steps:`")
    payload.setdefault("name", name_hint or "unnamed")

    try:
        flow = Flow.model_validate(payload)
    except ValidationError as e:
        raise FlowLoadError(f"invalid flow:\n{_pretty(e)}") from e

    bind(flow.steps, prefix="steps")
    bind(flow.on_failure, prefix="on_failure")
    return flow


def bind(steps: list[Step], prefix: str = "steps") -> None:
    """Attach the registry entry + parsed params to each step, recursing into blocks."""
    for i, step in enumerate(steps):
        where = f"{prefix}[{i}]"
        spec = get_action(step.action)
        step.spec = spec
        step.parsed = spec.validate(step.raw, where=where)
        for child_list_name, children in _child_blocks(step):
            bind(children, prefix=f"{where}.{step.action}.{child_list_name}")


def _child_blocks(step: Step) -> list[tuple[str, list[Step]]]:
    if not step.spec.control:
        return []
    out: list[tuple[str, list[Step]]] = []
    for field, value in step.parsed.__dict__.items():
        if isinstance(value, list) and value and all(isinstance(v, Step) for v in value):
            out.append((field, value))
    return out


def dump_schema() -> dict[str, Any]:
    """JSON Schema for the whole DSL — feed it to an editor for YAML autocompletion."""
    from .registry import REGISTRY

    return {
        "flow": Flow.model_json_schema(),
        "actions": {
            name: {"doc": spec.doc, "params": spec.model.model_json_schema()}
            for name, spec in sorted(REGISTRY.items())
        },
    }


def _pretty(e: ValidationError) -> str:
    return "\n".join(
        f"  - {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in e.errors()
    )


# `nested_steps` is re-exported for callers that walk a bound flow.
__all__ = ["load_flow", "bind", "dump_schema", "nested_steps"]
