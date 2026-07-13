"""Acting on elements: click, fill, select, upload, scroll."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from ..errors import ActionError
from ..registry import action
from ._common import RunContext, Selector, Step, Strict, loc, tmo

Modifier = Literal["Alt", "Control", "Meta", "Shift"]


class Click(Strict):
    selector: Selector
    button: Literal["left", "right", "middle"] = "left"
    count: int = 1
    modifiers: list[Modifier] = []
    force: bool = False
    delay: int = 0


@action("click", Click, shorthand="selector")
async def click(ctx: RunContext, p: Click, step: Step) -> None:
    """Click an element (waits for it to be actionable)."""
    await loc(ctx, p.selector).click(
        button=p.button,
        click_count=p.count,
        modifiers=p.modifiers or None,
        force=p.force,
        delay=p.delay,
        timeout=tmo(ctx, step),
    )


@action("dblclick", Click, shorthand="selector", aliases=("double_click",))
async def dblclick(ctx: RunContext, p: Click, step: Step) -> None:
    """Double-click an element."""
    await loc(ctx, p.selector).dblclick(
        button=p.button, modifiers=p.modifiers or None, force=p.force, timeout=tmo(ctx, step)
    )


class Fill(Strict):
    selector: Selector
    # Required on purpose: a `fill` that forgot its value would silently clear the
    # input. Write `value: ""` when clearing is what you actually meant.
    # Scalars are allowed because `"{{ vars.count }}"` renders to an int, not a str.
    value: str | int | float | bool


@action("fill", Fill)
async def fill(ctx: RunContext, p: Fill, step: Step) -> None:
    """Set the value of an input in one shot (clears whatever was there)."""
    await loc(ctx, p.selector).fill(str(p.value), timeout=tmo(ctx, step))


class Type(Strict):
    selector: Selector
    text: str | int | float | bool
    delay: int = 30  # ms between keystrokes


@action("type", Type)
async def type_(ctx: RunContext, p: Type, step: Step) -> None:
    """Type key by key — use when a site listens for keyboard events."""
    await loc(ctx, p.selector).press_sequentially(
        str(p.text), delay=p.delay, timeout=tmo(ctx, step)
    )


class Press(Strict):
    key: str
    selector: Selector | None = None  # defaults to the focused element


@action("press", Press, shorthand="key")
async def press(ctx: RunContext, p: Press, step: Step) -> None:
    """Press a key, e.g. ``Enter``, ``Escape``, ``Control+A``."""
    if p.selector is None:
        await ctx.page.keyboard.press(p.key)
    else:
        await loc(ctx, p.selector).press(p.key, timeout=tmo(ctx, step))


class Select(Strict):
    selector: Selector
    value: str | list[str] | None = None
    label: str | list[str] | None = None
    index: int | list[int] | None = None

    @model_validator(mode="after")
    def _one_of(self) -> Select:
        if sum(x is not None for x in (self.value, self.label, self.index)) != 1:
            raise ValueError("select needs exactly one of `value`, `label` or `index`")
        return self


@action("select", Select, aliases=("select_option",))
async def select(ctx: RunContext, p: Select, step: Step) -> list[str]:
    """Pick option(s) in a ``<select>``."""
    target = loc(ctx, p.selector)
    if p.value is not None:
        return await target.select_option(value=p.value, timeout=tmo(ctx, step))
    if p.label is not None:
        return await target.select_option(label=p.label, timeout=tmo(ctx, step))
    return await target.select_option(index=p.index, timeout=tmo(ctx, step))


class Toggle(Strict):
    selector: Selector


@action("check", Toggle, shorthand="selector")
async def check(ctx: RunContext, p: Toggle, step: Step) -> None:
    """Tick a checkbox or radio."""
    await loc(ctx, p.selector).check(timeout=tmo(ctx, step))


@action("uncheck", Toggle, shorthand="selector")
async def uncheck(ctx: RunContext, p: Toggle, step: Step) -> None:
    """Untick a checkbox."""
    await loc(ctx, p.selector).uncheck(timeout=tmo(ctx, step))


@action("hover", Toggle, shorthand="selector")
async def hover(ctx: RunContext, p: Toggle, step: Step) -> None:
    """Hover an element — often what reveals a dropdown."""
    await loc(ctx, p.selector).hover(timeout=tmo(ctx, step))


@action("focus", Toggle, shorthand="selector")
async def focus(ctx: RunContext, p: Toggle, step: Step) -> None:
    """Focus an element."""
    await loc(ctx, p.selector).focus(timeout=tmo(ctx, step))


class Upload(Strict):
    selector: Selector
    files: str | list[str]


@action("upload", Upload, aliases=("set_input_files",))
async def upload(ctx: RunContext, p: Upload, step: Step) -> None:
    """Attach files to an ``<input type=file>``."""
    files = [p.files] if isinstance(p.files, str) else p.files
    await loc(ctx, p.selector).set_input_files(files, timeout=tmo(ctx, step))


class Scroll(Strict):
    to: Literal["bottom", "top"] | None = None
    selector: Selector | None = None  # scroll this element into view
    by: int | None = None  # pixels; negative scrolls up

    @model_validator(mode="after")
    def _one_of(self) -> Scroll:
        if sum(x is not None for x in (self.to, self.selector, self.by)) != 1:
            raise ValueError("scroll needs exactly one of `to`, `selector` or `by`")
        return self


@action("scroll", Scroll, shorthand="to")
async def scroll(ctx: RunContext, p: Scroll, step: Step) -> None:
    """Scroll the page — the usual way to trigger lazy-loading feeds."""
    if p.selector is not None:
        await loc(ctx, p.selector).scroll_into_view_if_needed(timeout=tmo(ctx, step))
    elif p.by is not None:
        await ctx.page.mouse.wheel(0, p.by)
    elif p.to == "bottom":
        await ctx.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    elif p.to == "top":
        await ctx.page.evaluate("window.scrollTo(0, 0)")
    else:  # pragma: no cover - the validator makes this unreachable
        raise ActionError("scroll: nothing to do")
