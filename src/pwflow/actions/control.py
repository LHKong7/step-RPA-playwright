"""Control flow: if / foreach / repeat / while / try.

These are the only actions that own nested steps, so they are registered with
``control=True``: the engine hands them their payload *unrendered* and they render
their own condition on every pass. That is what makes ``while: "{{ ... }}"`` re-evaluate
instead of freezing at its load-time value.

`in`, `else` and `finally` are Python keywords, so the models spell them ``in_`` /
``else_`` / ``finally_`` and alias them back to the YAML names.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ..errors import ActionError, BreakLoop, ContinueLoop, StopFlow
from ..executor import run_steps
from ..models import Step
from ..registry import action
from ._common import NoParams, RunContext, Strict


class If(Strict):
    cond: str
    then: list[Step] = Field(default_factory=list)
    else_: list[Step] = Field(default_factory=list, alias="else")


@action("if", If, control=True, aliases=("branch",))
async def if_(ctx: RunContext, p: If, step: Step) -> bool:
    """Run `then:` or `else:` depending on a condition."""
    taken = ctx.truthy(p.cond)
    branch = p.then if taken else p.else_
    await run_steps(ctx, branch, prefix=f"{ctx.current_index}.{'then' if taken else 'else'}")
    return taken


class Foreach(Strict):
    in_: str | list[Any] = Field(alias="in")
    as_: str = Field(default="item", alias="as")
    index_as: str = "index"
    steps: list[Step]


@action("foreach", Foreach, control=True, aliases=("for_each",))
async def foreach(ctx: RunContext, p: Foreach, step: Step) -> int:
    """Run a block once per item of a list."""
    items = ctx.render(p.in_) if isinstance(p.in_, str) else p.in_
    if items is None:
        items = []
    if not isinstance(items, (list, tuple)):
        raise ActionError(f"foreach `in:` must resolve to a list, got {type(items).__name__}")

    done = 0
    prefix = ctx.current_index
    for i, item in enumerate(items):
        with ctx.locals(**{p.as_: item, p.index_as: i}):
            try:
                await run_steps(ctx, p.steps, prefix=f"{prefix}.{i}")
            except ContinueLoop:
                continue
            except BreakLoop:
                break
        done += 1
    return done


def _int(ctx: RunContext, value: int | str, field: str) -> int:
    """Control fields are handed over unrendered, so a count may still be a template."""
    rendered = ctx.render(value) if isinstance(value, str) else value
    try:
        return int(rendered)
    except (TypeError, ValueError) as e:
        raise ActionError(f"`{field}:` must be an integer, got {rendered!r}") from e


class Repeat(Strict):
    times: int | str
    as_: str = Field(default="index", alias="as")
    steps: list[Step]


@action("repeat", Repeat, control=True)
async def repeat(ctx: RunContext, p: Repeat, step: Step) -> int:
    """Run a block a fixed number of times."""
    times = _int(ctx, p.times, "times")

    done = 0
    prefix = ctx.current_index
    for i in range(times):
        with ctx.locals(**{p.as_: i}):
            try:
                await run_steps(ctx, p.steps, prefix=f"{prefix}.{i}")
            except ContinueLoop:
                continue
            except BreakLoop:
                break
        done += 1
    return done


class While(Strict):
    cond: str
    # hard stop; a scraper that loops forever is a bug, not a feature
    max: int | str = 100
    as_: str = Field(default="index", alias="as")
    steps: list[Step]


@action("while", While, control=True)
async def while_(ctx: RunContext, p: While, step: Step) -> int:
    """Run a block while a condition holds — the natural shape for "next page" pagination."""
    limit = _int(ctx, p.max, "max")
    done = 0
    prefix = ctx.current_index
    while done < limit:
        if not ctx.truthy(p.cond):
            break
        with ctx.locals(**{p.as_: done}):
            try:
                await run_steps(ctx, p.steps, prefix=f"{prefix}.{done}")
            except ContinueLoop:
                done += 1
                continue
            except BreakLoop:
                done += 1
                break
        done += 1
    return done


class Try(Strict):
    steps: list[Step]
    catch: list[Step] = Field(default_factory=list)
    finally_: list[Step] = Field(default_factory=list, alias="finally")


@action("try", Try, control=True)
async def try_(ctx: RunContext, p: Try, step: Step) -> bool:
    """Run a block; on failure run `catch:` instead of aborting. `error` is in scope there."""
    prefix = ctx.current_index
    ok = True
    try:
        await run_steps(ctx, p.steps, prefix=f"{prefix}.try")
    except (BreakLoop, ContinueLoop, StopFlow):
        raise
    except Exception as e:  # noqa: BLE001 - that is the point of `try`
        ok = False
        with ctx.locals(error=str(e)):
            await run_steps(ctx, p.catch, prefix=f"{prefix}.catch")
    finally:
        if p.finally_:
            await run_steps(ctx, p.finally_, prefix=f"{prefix}.finally")
    return ok


class Block(Strict):
    steps: list[Step]


@action("block", Block, shorthand="steps", control=True, aliases=("group",))
async def block(ctx: RunContext, p: Block, step: Step) -> None:
    """Group steps so a single `when:` or `retry:` covers all of them."""
    await run_steps(ctx, p.steps, prefix=ctx.current_index)


@action("break", NoParams, control=True)
async def break_(ctx: RunContext, p: NoParams, step: Step) -> None:
    """Leave the enclosing loop. Pair it with `when:`."""
    raise BreakLoop()


@action("continue", NoParams, control=True)
async def continue_(ctx: RunContext, p: NoParams, step: Step) -> None:
    """Skip to the next iteration of the enclosing loop."""
    raise ContinueLoop()


@action("stop", NoParams, control=True)
async def stop(ctx: RunContext, p: NoParams, step: Step) -> None:
    """End the run early, successfully."""
    raise StopFlow()
