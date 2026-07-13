"""Assertions.

A failed assertion is fatal on purpose: it is never retried and `optional: true`
does not swallow it. If a scrape's shape guarantee breaks, you want the run to stop
and tell you — not to quietly write a file with three rows in it.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from playwright.async_api import expect
from pydantic import model_validator

from ..errors import AssertionFailed
from ..registry import action
from ._common import RunContext, Selector, Step, Strict, loc, tmo


class Assert(Strict):
    # any subset may be combined; all of them must hold
    #
    # `expr` is Any, not str, on purpose: the engine renders params before an action
    # sees them, so `assert: "{{ 1 == 2 }}"` arrives here as the bool False, not as a
    # template. Typing it `str` made a *failed assertion* look like a params error —
    # which `optional: true` would then have swallowed.
    expr: Any = None
    selector: Selector | None = None
    state: Literal["visible", "hidden", "attached", "detached"] | None = None
    text: str | None = None  # `selector` contains this text (or the page does)
    url: str | None = None  # glob, or `re:`-prefixed regex
    count: int | None = None  # exact number of matches for `selector`
    min_count: int | None = None
    message: str | None = None

    @model_validator(mode="after")
    def _something_to_check(self) -> Assert:
        checks = (self.expr, self.selector, self.text, self.url, self.count, self.min_count)
        if all(c is None for c in checks):
            raise ValueError(
                "assert needs at least one of: expr, selector, text, url, count, min_count"
            )
        if self.state is not None and self.selector is None:
            raise ValueError("`state` needs a `selector`")
        return self


@action("assert", Assert, shorthand="expr", aliases=("expect",))
async def assert_(ctx: RunContext, p: Assert, step: Step) -> bool:
    """Fail the run unless every given condition holds."""
    timeout = tmo(ctx, step)

    def fail(detail: str) -> None:
        raise AssertionFailed(p.message or detail, step=step.label)

    if p.expr is not None:
        # already-rendered values (bool, int, list) are judged on truthiness; a bare
        # string like "vars.n > 1" is still an expression and gets evaluated.
        held = ctx.truthy(p.expr) if isinstance(p.expr, str) else bool(p.expr)
        if not held:
            fail(f"expression is falsy: {p.expr!r}")

    if p.url is not None:
        matcher: Any = re.compile(p.url[3:]) if p.url.startswith("re:") else p.url
        try:
            await expect(ctx.page).to_have_url(matcher, timeout=timeout)
        except AssertionError:
            fail(f"url {ctx.page.url!r} does not match {p.url!r}")

    if p.selector is not None:
        target = loc(ctx, p.selector)
        if p.state is not None:
            try:
                await target.first.wait_for(state=p.state, timeout=timeout)
            except Exception:  # noqa: BLE001 - playwright raises TimeoutError here
                fail(f"selector {p.selector!r} never became {p.state}")
        if p.count is not None:
            try:
                await expect(target).to_have_count(p.count, timeout=timeout)
            except AssertionError:
                fail(f"expected {p.count} matches for {p.selector!r}, got {await target.count()}")
        if p.min_count is not None:
            n = await target.count()
            if n < p.min_count:
                fail(f"expected at least {p.min_count} matches for {p.selector!r}, got {n}")
        if p.text is not None:
            try:
                await expect(target.first).to_contain_text(p.text, timeout=timeout)
            except AssertionError:
                fail(f"{p.selector!r} does not contain {p.text!r}")
    elif p.text is not None:
        if p.text not in await ctx.page.content():
            fail(f"page does not contain {p.text!r}")

    # `state: visible` with no other check is the common "the page loaded" assertion.
    if p.selector is not None and p.state is None and p.count is None and p.min_count is None \
            and p.text is None:
        if await loc(ctx, p.selector).count() == 0:
            fail(f"selector {p.selector!r} matched nothing")

    return True
