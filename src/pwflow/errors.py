"""Error types. Every failure a flow author can cause should land in one of these."""

from __future__ import annotations


class PwFlowError(Exception):
    """Base class for everything this package raises deliberately."""


class FlowLoadError(PwFlowError):
    """The YAML is not a valid flow (bad shape, unknown action, unknown key)."""


class TemplateError(PwFlowError):
    """A ``{{ ... }}`` expression failed to render."""


class SelectorError(PwFlowError):
    """A selector could not be turned into a Playwright locator."""


class ActionError(PwFlowError):
    """An action failed at runtime. Carries the step that raised it."""

    def __init__(self, message: str, *, step: str | None = None, index: int | None = None):
        self.step = step
        self.index = index
        super().__init__(message)

    def __str__(self) -> str:
        where = f"step {self.index}" if self.index is not None else "step"
        if self.step:
            where += f" ({self.step})"
        return f"{where}: {super().__str__()}"


class AssertionFailed(ActionError):
    """An ``assert`` step did not hold. Always fatal, even under ``optional``."""


# Control-flow signals. Not errors — they unwind the step loop.


class BreakLoop(PwFlowError):
    pass


class ContinueLoop(PwFlowError):
    pass


class StopFlow(PwFlowError):
    """Ends the run early and reports success."""
