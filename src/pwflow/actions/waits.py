"""Waiting.

Playwright already auto-waits before every action, so reach for these only when you
need to wait for something you are *not* about to click: a spinner disappearing, the
network going quiet, a redirect landing.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal

from ..registry import action
from ._common import RunContext, Selector, Step, Strict, loc, tmo


class WaitFor(Strict):
    selector: Selector
    state: Literal["attached", "detached", "visible", "hidden"] = "visible"


@action("wait_for", WaitFor, shorthand="selector", aliases=("wait_for_selector",))
async def wait_for(ctx: RunContext, p: WaitFor, step: Step) -> None:
    """Wait until an element reaches a state."""
    # `.first`, because `wait_for: ".row"` means "wait for the rows to show up" — and
    # Playwright's strict mode would otherwise reject the very selector you want to wait on.
    await loc(ctx, p.selector).first.wait_for(state=p.state, timeout=tmo(ctx, step))


class WaitForUrl(Strict):
    url: str  # glob (``**/items/*``) or ``re:`` prefix for a regex


@action("wait_for_url", WaitForUrl, shorthand="url")
async def wait_for_url(ctx: RunContext, p: WaitForUrl, step: Step) -> str:
    """Wait until the page URL matches a glob or ``re:`` pattern."""
    matcher = re.compile(p.url[3:]) if p.url.startswith("re:") else p.url
    await ctx.page.wait_for_url(matcher, timeout=tmo(ctx, step))
    return ctx.page.url


class WaitForLoadState(Strict):
    state: Literal["load", "domcontentloaded", "networkidle"] = "networkidle"


@action("wait_for_load", WaitForLoadState, shorthand="state", aliases=("wait_for_load_state",))
async def wait_for_load(ctx: RunContext, p: WaitForLoadState, step: Step) -> None:
    """Wait for the page's load state — ``networkidle`` is the usual one for scraping."""
    await ctx.page.wait_for_load_state(p.state, timeout=tmo(ctx, step))


class WaitForFunction(Strict):
    script: str
    poll: int | None = None  # ms between polls; default is on animation frame


@action("wait_for_function", WaitForFunction, shorthand="script")
async def wait_for_function(ctx: RunContext, p: WaitForFunction, step: Step) -> None:
    """Wait until a JS expression turns truthy in the page."""
    await ctx.page.wait_for_function(
        p.script, timeout=tmo(ctx, step), polling=p.poll or "raf"
    )


class Sleep(Strict):
    ms: int


@action("sleep", Sleep, shorthand="ms", aliases=("wait",))
async def sleep(ctx: RunContext, p: Sleep, step: Step) -> None:
    """Sleep. A blunt instrument — prefer `wait_for` when you can name the condition."""
    await asyncio.sleep(p.ms / 1000)
