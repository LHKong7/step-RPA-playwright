"""pwflow — declarative Playwright automation.

    from pwflow import run_flow
    result = await run_flow("flows/hn.yaml", vars={"pages": 3})
    print(result.data["stories"])
"""

from .context import RunResult, StepReport
from .engine import Engine, run_flow
from .errors import ActionError, AssertionFailed, FlowLoadError, PwFlowError
from .loader import load_flow
from .metrics import METRICS
from .models import Flow, Step
from .observability import bind_context, configure_logging
from .registry import action

__version__ = "0.1.0"
__all__ = [
    "METRICS",
    "ActionError",
    "AssertionFailed",
    "Engine",
    "Flow",
    "FlowLoadError",
    "PwFlowError",
    "RunResult",
    "Step",
    "StepReport",
    "action",
    "bind_context",
    "configure_logging",
    "load_flow",
    "run_flow",
]
