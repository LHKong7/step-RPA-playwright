"""Importing this package registers every built-in action.

To add one of your own, drop a module here (or anywhere) that calls
:func:`pwflow.registry.action`, and import it before loading a flow::

    from pwflow.registry import action
    from pwflow.actions._common import RunContext, Step, Strict

    class Solve(Strict):
        sitekey: str

    @action("solve_captcha", Solve)
    async def solve_captcha(ctx: RunContext, p: Solve, step: Step) -> str:
        ...
"""

from . import assertions, control, extract, interaction, misc, navigation, waits  # noqa: F401

__all__ = ["assertions", "control", "extract", "interaction", "misc", "navigation", "waits"]
