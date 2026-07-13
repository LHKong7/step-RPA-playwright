"""pwflow — declarative Playwright automation.

    from pwflow import run_flow
    result = await run_flow("flows/hn.yaml", vars={"pages": 3})
    print(result.data["stories"])
"""

from .context import RunResult, StepReport
from .engine import Engine, run_flow
from .errors import ActionError, AssertionFailed, FlowLoadError, PwFlowError
from .loader import load_flow
from .models import Flow, Step
from .registry import action

__version__ = "0.1.0"
__all__ = [
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
    "load_flow",
    "run_flow",
]
