"""Browser lifecycle and the run entrypoint.

One ``Engine`` owns one Playwright process. Stock (``provider: playwright``) browsers are
pooled by ``(engine, headless, slow_mo)`` and shared, so the HTTP service serves many
runs without paying the ~300ms launch each time. CloakBrowser and CDP browsers are *not*
pooled — each run gets its own (a shared CloakBrowser process would share a fingerprint
seed) and closes it on the way out. Either way each run gets its own ``BrowserContext``,
so concurrent runs never see each other's cookies.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from .actions.misc import serialize
from .browsers import Acquired, acquire
from .context import RunContext, RunResult
from .errors import StopFlow
from .executor import run_steps
from .loader import load_flow
from .models import BrowserConfig, Flow
from .template import render_deep

log = logging.getLogger("pwflow")

_RESOURCE_TYPES = {"image", "font", "stylesheet", "media", "script", "xhr", "fetch"}


class Engine:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browsers: dict[tuple, Browser] = {}

    async def start(self) -> Engine:
        if self._pw is None:
            self._pw = await async_playwright().start()
        return self

    async def close(self) -> None:
        for browser in self._browsers.values():
            try:
                await browser.close()
            except Exception:  # noqa: BLE001 - a pooled browser may already be gone
                pass
        self._browsers.clear()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def __aenter__(self) -> Engine:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _context(self, acq: Acquired, cfg: BrowserConfig, artifacts: Path) -> BrowserContext:
        skip = acq.launch_level  # options the provider already applied at launch/connect
        options: dict[str, Any] = {
            "viewport": {"width": cfg.viewport.width, "height": cfg.viewport.height},
            "ignore_https_errors": cfg.ignore_https_errors,
        }
        if cfg.user_agent:
            options["user_agent"] = cfg.user_agent
        if cfg.locale and "locale" not in skip:
            options["locale"] = cfg.locale
        if cfg.timezone and "timezone" not in skip:
            options["timezone_id"] = cfg.timezone
        if cfg.extra_http_headers:
            options["extra_http_headers"] = cfg.extra_http_headers
        if cfg.proxy and "proxy" not in skip:
            options["proxy"] = cfg.proxy.model_dump(exclude_none=True)
        if cfg.storage_state and Path(cfg.storage_state).exists():
            options["storage_state"] = cfg.storage_state
        if cfg.record_video:
            options["record_video_dir"] = str(artifacts / "video")

        context = await acq.browser.new_context(**options)
        context.set_default_timeout(cfg.timeout)
        context.set_default_navigation_timeout(cfg.navigation_timeout)

        if cfg.block_resources:
            blocked = set(cfg.block_resources) & _RESOURCE_TYPES

            async def _route(route, request):  # type: ignore[no-untyped-def]
                if request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", _route)

        if cfg.trace:
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        return context

    # -- the run -----------------------------------------------------------

    async def run(
        self,
        flow: Flow | str | Path,
        *,
        vars: dict[str, Any] | None = None,
        run_id: str | None = None,
        artifacts_dir: Path | None = None,
    ) -> RunResult:
        if not isinstance(flow, Flow):
            flow = load_flow(flow)

        assert self._pw is not None, "Engine.start() was not awaited"
        started = time.time()
        run_id = run_id or uuid.uuid4().hex[:12]
        artifacts = artifacts_dir or Path(flow.output.artifacts_dir) / run_id

        merged_vars = {**flow.vars, **(vars or {})}
        cfg = _render_browser_config(flow.browser, merged_vars)

        acq = await acquire(self._pw, self._browsers, cfg)
        browser_context = await self._context(acq, cfg, artifacts)
        page = await browser_context.new_page()
        ctx = RunContext(
            flow, page, browser_context, run_id=run_id, artifacts_dir=artifacts, overrides=vars
        )

        status: str = "success"
        error: str | None = None
        max_duration = flow.limits.max_duration
        try:
            steps = run_steps(ctx, flow.steps)
            # wait_for cancels the step loop on timeout; teardown stays outside this scope
            # (in the finally) so a cancellation mid-step still tears the browser down.
            if max_duration:
                await asyncio.wait_for(steps, timeout=max_duration)
            else:
                await steps
        except StopFlow:
            log.info("flow stopped early by `stop:`")
        except Exception as e:  # noqa: BLE001 - the run result carries the failure
            status = "failed"
            error = (
                f"run exceeded max_duration ({max_duration}s)"
                if isinstance(e, TimeoutError)
                else str(e)
            )
            log.error("run %s failed: %s", ctx.run_id, error)
            if flow.on_failure:
                try:
                    with ctx.locals(error=error):
                        await run_steps(ctx, flow.on_failure, prefix="on_failure")
                except Exception as cleanup_error:  # noqa: BLE001
                    log.error("on_failure itself failed: %s", cleanup_error)
        finally:
            await self._teardown(ctx, cfg, acq, artifacts)

        if flow.output.path and status == "success":
            _write_output(ctx, flow)

        return RunResult(
            run_id=ctx.run_id,
            flow=flow.name,
            status=status,  # type: ignore[arg-type]
            started_at=started,
            finished_at=time.time(),
            data=ctx.data,
            steps=ctx.reports,
            artifacts=ctx.artifacts,
            error=error,
            warnings=ctx.warnings,
        )

    async def _teardown(
        self, ctx: RunContext, cfg: BrowserConfig, acq: Acquired, artifacts: Path
    ) -> None:
        bc = ctx.browser_context

        # Each teardown step is a side artifact — a failure must be recorded, not lost.
        # Doing them in a `try` whose `finally` closes the context (as before) let a
        # save-session failure vanish behind close(), so a run reported success with no
        # saved session and no way to know.
        if cfg.save_storage_state:
            try:
                path = Path(cfg.save_storage_state)
                path.parent.mkdir(parents=True, exist_ok=True)
                await bc.storage_state(path=str(path))
                log.info("saved session to %s", path)
            except Exception as e:  # noqa: BLE001
                ctx.warnings.append(f"save_storage_state to {cfg.save_storage_state!r} failed: {e}")
        if cfg.trace:
            try:
                artifacts.mkdir(parents=True, exist_ok=True)
                trace = artifacts / "trace.zip"
                await bc.tracing.stop(path=str(trace))
                ctx.artifacts.append(str(trace))
            except Exception as e:  # noqa: BLE001
                ctx.warnings.append(f"trace stop failed: {e}")

        try:
            await bc.close()
        except Exception:  # noqa: BLE001
            pass
        if acq.owned:
            # a per-run CloakBrowser process or CDP connection — never pooled
            try:
                await acq.browser.close()
            except Exception:  # noqa: BLE001
                pass


def _render_browser_config(cfg: BrowserConfig, vars: dict[str, Any]) -> BrowserConfig:
    """Resolve ``{{ env.X }}`` / ``{{ vars.x }}`` in the browser block before launch.

    The browser is built before any step runs, so it cannot use the full step scope —
    but ``vars`` and ``env`` are known, and that is enough to keep secrets (proxy
    passwords, a CloakBrowser license key) in the environment instead of in the YAML.
    """
    scope = {"vars": vars, "env": dict(os.environ)}
    rendered = render_deep(cfg.model_dump(mode="python"), scope)
    return BrowserConfig.model_validate(rendered)


def _write_output(ctx: RunContext, flow: Flow) -> None:
    payload = ctx.data if flow.output.key is None else ctx.data.get(flow.output.key)
    path = Path(str(ctx.render(flow.output.path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize(payload, flow.output.format), encoding="utf-8")
    ctx.artifacts.append(str(path))
    log.info("wrote %s", path)


async def run_flow(
    flow: Flow | str | Path, *, vars: dict[str, Any] | None = None
) -> RunResult:
    """Convenience one-shot: start an engine, run a single flow, tear it all down."""
    async with Engine() as engine:
        return await engine.run(flow, vars=vars)
