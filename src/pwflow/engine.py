"""Browser lifecycle and the run entrypoint.

One ``Engine`` owns one Playwright process and a pool of browsers keyed by
``(engine, headless, slow_mo)``. Each *run* gets its own ``BrowserContext`` — a fresh
cookie jar and cache — so two concurrent runs of the same flow cannot see each other's
session. That is the split that lets the HTTP service serve many runs without paying
the ~300ms browser launch every time.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright

from .actions.misc import serialize
from .context import RunContext, RunResult
from .errors import StopFlow
from .executor import run_steps
from .loader import load_flow
from .models import BrowserConfig, Flow

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
            await browser.close()
        self._browsers.clear()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def __aenter__(self) -> Engine:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _browser(self, cfg: BrowserConfig) -> Browser:
        assert self._pw is not None, "Engine.start() was not awaited"
        key = (cfg.engine, cfg.headless, cfg.slow_mo)
        if key not in self._browsers:
            launcher = getattr(self._pw, cfg.engine)
            self._browsers[key] = await launcher.launch(
                headless=cfg.headless, slow_mo=cfg.slow_mo
            )
            log.debug("launched %s (headless=%s)", cfg.engine, cfg.headless)
        return self._browsers[key]

    async def _context(self, cfg: BrowserConfig, artifacts: Path) -> BrowserContext:
        browser = await self._browser(cfg)
        options: dict[str, Any] = {
            "viewport": {"width": cfg.viewport.width, "height": cfg.viewport.height},
            "ignore_https_errors": cfg.ignore_https_errors,
        }
        if cfg.user_agent:
            options["user_agent"] = cfg.user_agent
        if cfg.locale:
            options["locale"] = cfg.locale
        if cfg.timezone:
            options["timezone_id"] = cfg.timezone
        if cfg.extra_http_headers:
            options["extra_http_headers"] = cfg.extra_http_headers
        if cfg.proxy:
            options["proxy"] = cfg.proxy.model_dump(exclude_none=True)
        if cfg.storage_state and Path(cfg.storage_state).exists():
            options["storage_state"] = cfg.storage_state
        if cfg.record_video:
            options["record_video_dir"] = str(artifacts / "video")

        context = await browser.new_context(**options)
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

        started = time.time()
        cfg = flow.browser
        run_id = run_id or uuid.uuid4().hex[:12]
        artifacts = artifacts_dir or Path(flow.output.artifacts_dir) / run_id

        browser_context = await self._context(cfg, artifacts)
        page = await browser_context.new_page()
        ctx = RunContext(
            flow, page, browser_context, run_id=run_id, artifacts_dir=artifacts, overrides=vars
        )

        status: str = "success"
        error: str | None = None
        try:
            await run_steps(ctx, flow.steps)
        except StopFlow:
            log.info("flow stopped early by `stop:`")
        except Exception as e:  # noqa: BLE001 - the run result carries the failure
            status, error = "failed", str(e)
            log.error("run %s failed: %s", ctx.run_id, e)
            if flow.on_failure:
                try:
                    with ctx.locals(error=str(e)):
                        await run_steps(ctx, flow.on_failure, prefix="on_failure")
                except Exception as cleanup_error:  # noqa: BLE001
                    log.error("on_failure itself failed: %s", cleanup_error)
        finally:
            await self._teardown(ctx, cfg, browser_context, artifacts)

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
        )

    async def _teardown(
        self, ctx: RunContext, cfg: BrowserConfig, bc: BrowserContext, artifacts: Path
    ) -> None:
        try:
            if cfg.save_storage_state:
                path = Path(cfg.save_storage_state)
                path.parent.mkdir(parents=True, exist_ok=True)
                await bc.storage_state(path=str(path))
                log.info("saved session to %s", path)
            if cfg.trace:
                artifacts.mkdir(parents=True, exist_ok=True)
                trace = artifacts / "trace.zip"
                await bc.tracing.stop(path=str(trace))
                ctx.artifacts.append(str(trace))
        finally:
            await bc.close()


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
