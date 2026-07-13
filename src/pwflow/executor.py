"""The step loop: condition, retry, dispatch, report.

Kept apart from :mod:`pwflow.engine` (which owns the browser) so that control-flow
actions can recurse into it without importing the world.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from .context import RunContext, StepReport
from .errors import ActionError, AssertionFailed, BreakLoop, ContinueLoop, StopFlow
from .models import Step

log = logging.getLogger("pwflow")


async def run_steps(ctx: RunContext, steps: list[Step], prefix: str = "") -> None:
    for i, step in enumerate(steps):
        index = f"{prefix}{i}" if not prefix else f"{prefix}.{i}"
        await run_step(ctx, step, index)


async def run_step(ctx: RunContext, step: Step, index: str) -> Any:
    started = time.perf_counter()

    if step.when is not None and not ctx.truthy(step.when):
        ctx.reports.append(
            StepReport(index, step.action, step.label, "skipped", _ms(started))
        )
        log.debug("[%s] %s skipped (when: %s)", index, step.action, step.when)
        return None

    retry = step.retry
    max_attempts = 1 + (retry.times if retry else 0)
    delay = (retry.delay / 1000) if retry else 0.0
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = await _dispatch(ctx, step, index)
        except (BreakLoop, ContinueLoop, StopFlow):
            raise  # control-flow signals are not failures
        except AssertionFailed:
            raise  # a broken assertion is never retried or swallowed
        except Exception as e:  # noqa: BLE001 - actions raise anything a page can throw
            last_error = e
            if attempt < max_attempts:
                log.warning(
                    "[%s] %s failed (attempt %d/%d): %s — retrying in %.1fs",
                    index, step.action, attempt, max_attempts, _brief(e), delay,
                )
                await asyncio.sleep(delay)
                delay *= retry.backoff if retry else 1.0
                continue
            break
        else:
            if step.id:
                ctx.step_outputs[step.id] = result
            status = "recovered" if attempt > 1 else "ok"
            ctx.reports.append(
                StepReport(index, step.action, step.label, status, _ms(started), attempt)
            )
            return result

    # every attempt failed
    message = _brief(last_error)
    ctx.reports.append(
        StepReport(
            index, step.action, step.label,
            "failed", _ms(started), max_attempts, error=message,
        )
    )
    if step.optional:
        log.warning("[%s] %s failed but is optional: %s", index, step.action, message)
        return None
    raise ActionError(message, step=step.label, index=None) from last_error


async def _dispatch(ctx: RunContext, step: Step, index: str) -> Any:
    spec = step.spec
    ctx.current_index = index
    if spec.control:
        # Nested bodies must keep their templates unrendered — the block re-evaluates
        # them on every iteration — so control actions get the params parsed at load.
        return await spec.impl(ctx, step.parsed, step)

    payload = ctx.render_deep(spec.normalize(step.raw))
    try:
        params = spec.model.model_validate(payload)
    except ValidationError as e:
        raise ActionError(f"`{spec.name}` params are invalid after rendering: {e}") from e

    try:
        return await spec.impl(ctx, params, step)
    except PlaywrightError as e:
        raise ActionError(f"`{spec.name}`: {_brief(e)}") from e


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _brief(e: Exception | None) -> str:
    if e is None:
        return "unknown error"
    text = str(e).strip()
    # Playwright errors carry a long "Call log" tail that buries the actual message.
    for marker in ("\nCall log:", "\n=========================", "\nCall Log:"):
        if marker in text:
            text = text.split(marker, 1)[0]
    first = text.splitlines()[0] if text else type(e).__name__
    return f"{type(e).__name__}: {first}" if not isinstance(e, ActionError) else first
