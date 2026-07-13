"""Moving between pages."""

from __future__ import annotations

from typing import Literal

from ..registry import action
from ._common import NoParams, RunContext, Step, Strict

WaitUntil = Literal["load", "domcontentloaded", "networkidle", "commit"]


class Goto(Strict):
    url: str
    wait_until: WaitUntil = "load"
    referer: str | None = None


@action("goto", Goto, shorthand="url", aliases=("open", "visit"))
async def goto(ctx: RunContext, p: Goto, step: Step) -> str:
    """Navigate to a URL."""
    timeout = float(step.timeout or ctx.flow.browser.navigation_timeout)
    await ctx.page.goto(p.url, wait_until=p.wait_until, referer=p.referer, timeout=timeout)
    return ctx.page.url


@action("back", NoParams)
async def back(ctx: RunContext, p: NoParams, step: Step) -> str:
    """Go back in history."""
    await ctx.page.go_back(timeout=float(step.timeout or ctx.flow.browser.navigation_timeout))
    return ctx.page.url


@action("forward", NoParams)
async def forward(ctx: RunContext, p: NoParams, step: Step) -> str:
    """Go forward in history."""
    await ctx.page.go_forward(timeout=float(step.timeout or ctx.flow.browser.navigation_timeout))
    return ctx.page.url


class Reload(Strict):
    wait_until: WaitUntil = "load"


@action("reload", Reload)
async def reload(ctx: RunContext, p: Reload, step: Step) -> str:
    """Reload the current page."""
    await ctx.page.reload(
        wait_until=p.wait_until,
        timeout=float(step.timeout or ctx.flow.browser.navigation_timeout),
    )
    return ctx.page.url
