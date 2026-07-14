"""Everything a step can see or touch while it runs."""

from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from playwright.async_api import BrowserContext, Page

from .models import Flow
from .template import render, render_deep, truthy

log = logging.getLogger("pwflow")

StepStatus = Literal["ok", "skipped", "failed", "recovered"]


@dataclass
class StepReport:
    index: str  # "3" at top level, "3.1.2" inside nested blocks
    action: str
    label: str
    status: StepStatus
    duration_ms: int
    attempts: int = 1
    error: str | None = None


@dataclass
class RunResult:
    run_id: str
    flow: str
    status: Literal["success", "failed"]
    started_at: float
    finished_at: float
    data: dict[str, Any] = field(default_factory=dict)
    steps: list[StepReport] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    # non-fatal problems: a `save_storage_state` that failed, a trace that would not stop.
    # The run still succeeded, but you should know these did not.
    warnings: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at) * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "flow": self.flow,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "warnings": self.warnings,
            "data": self.data,
            "artifacts": self.artifacts,
            "steps": [s.__dict__ for s in self.steps],
        }


class RunContext:
    """Carries the page, the variable scopes, and the collected data through a run.

    Template scope, outermost to innermost:

    ==============  ===========================================================
    ``vars``        flow ``vars:``, overridden by CLI ``--var`` / API ``vars``
    ``env``         process environment — where secrets belong
    ``data``        everything ``extract`` has collected so far
    ``steps.<id>``  the return value of a step that declared an ``id``
    ``flow``        ``name``, ``run_id``, ``artifacts_dir``
    ``page``        ``url`` and ``title`` of the live page
    *loop locals*   ``item`` / ``index`` from the enclosing ``foreach`` / ``repeat``
    ==============  ===========================================================
    """

    def __init__(
        self,
        flow: Flow,
        page: Page,
        browser_context: BrowserContext,
        *,
        run_id: str | None = None,
        artifacts_dir: Path | None = None,
        overrides: dict[str, Any] | None = None,
    ):
        self.flow = flow
        self.page = page
        self.browser_context = browser_context
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.artifacts_dir = artifacts_dir or Path(flow.output.artifacts_dir) / self.run_id

        self.vars: dict[str, Any] = {**flow.vars, **(overrides or {})}
        self.data: dict[str, Any] = {}
        self.step_outputs: dict[str, Any] = {}
        self.artifacts: list[str] = []
        self.warnings: list[str] = []
        self.reports: list[StepReport] = []

        self._locals: list[dict[str, Any]] = []
        self.started_at = time.time()
        self.current_index = ""  # dotted path of the running step, e.g. "3.1"; for nested reports

        # A stable base for the template scope, built once. `vars`/`data`/`step_outputs`
        # are live dict references, so mutations show through without a rebuild; `env` is
        # snapshotted (it does not change mid-run); `flow` is constant. Only `page.url` and
        # loop locals change per render, so `scope()` only overlays those. This matters
        # because scope() runs on every template render — once per extracted field.
        self._scope_base: dict[str, Any] = {
            "vars": self.vars,
            "env": dict(os.environ),
            "data": self.data,
            "steps": self.step_outputs,
            "flow": {
                "name": flow.name,
                "run_id": self.run_id,
                "artifacts_dir": str(self.artifacts_dir),
            },
        }

    # -- template scope ----------------------------------------------------

    def scope(self) -> dict[str, Any]:
        merged = dict(self._scope_base)  # shallow: 5 keys, all live references
        merged["page"] = {"url": self._page_url()}
        for frame in self._locals:
            merged.update(frame)
        return merged

    def _page_url(self) -> str:
        # `output.path` is rendered after teardown, so the page may already be gone.
        try:
            return self.page.url if self.page is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    @contextmanager
    def locals(self, **kwargs: Any):
        """Push loop variables (``item``, ``index``, ...) for the duration of a block."""
        self._locals.append(kwargs)
        try:
            yield
        finally:
            self._locals.pop()

    def render(self, value: Any) -> Any:
        return render(value, self.scope())

    def render_deep(self, value: Any) -> Any:
        return render_deep(value, self.scope())

    def truthy(self, expr: str) -> bool:
        return truthy(expr, self.scope())

    # -- collected state ---------------------------------------------------

    def put_data(self, name: str, value: Any, append: bool = False) -> None:
        if not append:
            self.data[name] = value
            return
        bucket = self.data.setdefault(name, [])
        if not isinstance(bucket, list):
            bucket = self.data[name] = [bucket]
        bucket.extend(value) if isinstance(value, list) else bucket.append(value)

    def artifact_path(self, filename: str) -> Path:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / filename
        self.artifacts.append(str(path))
        return path

    def timeout_for(self, step_timeout: int | None) -> float:
        return float(step_timeout if step_timeout is not None else self.flow.browser.timeout)
