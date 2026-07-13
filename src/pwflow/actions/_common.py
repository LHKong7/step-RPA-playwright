"""Shared pieces for action modules."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Locator

from ..context import RunContext
from ..models import Selector, Step, Strict
from ..selectors import resolve

__all__ = ["Any", "Locator", "NoParams", "RunContext", "Selector", "Step", "Strict", "loc", "tmo"]


class NoParams(Strict):
    """For actions that take no arguments (``back:``, ``break:``)."""


def loc(ctx: RunContext, selector: Selector, scope: Any = None) -> Locator:
    return resolve(scope if scope is not None else ctx.page, selector)


def tmo(ctx: RunContext, step: Step) -> float:
    """Per-step timeout in ms, falling back to ``browser.timeout``."""
    return ctx.timeout_for(step.timeout)
