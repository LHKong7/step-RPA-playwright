"""HTTP API.

Same engine as the CLI, wrapped in FastAPI. A caller either posts YAML inline or names
a flow from ``flows_dir``; either way they choose between waiting for the result
(``wait: true``, the default) and getting a ``run_id`` back immediately.

The browser pool lives for the life of the process, so a run costs a
``BrowserContext``, not a browser launch. ``concurrency`` caps how many run at once —
browsers are memory-hungry, and an unbounded queue of them is how a scraper takes down
its own host.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..engine import Engine
from ..errors import FlowLoadError
from ..loader import load_flow
from ..models import Flow
from ..registry import canonical

log = logging.getLogger("pwflow")


class RunRequest(BaseModel):
    flow: str | None = None  # name of a YAML file in flows_dir (without the extension)
    yaml: str | None = None  # or the flow source, inline
    vars: dict[str, Any] = Field(default_factory=dict)
    wait: bool = True  # false -> return a run_id and execute in the background
    headless: bool | None = None  # override, handy for debugging against a headed browser

    @model_validator(mode="after")
    def _one_source(self) -> RunRequest:
        if (self.flow is None) == (self.yaml is None):
            raise ValueError("provide exactly one of `flow` or `yaml`")
        return self


class RunRecord(BaseModel):
    run_id: str
    flow: str
    status: Literal["queued", "running", "success", "failed"]
    created_at: float
    duration_ms: int | None = None
    error: str | None = None
    data: dict[str, Any] | None = None
    artifacts: list[str] | None = None
    steps: list[dict[str, Any]] | None = None


def create_app(flows_dir: Path = Path("flows"), concurrency: int = 4) -> FastAPI:
    runs: dict[str, RunRecord] = {}
    engine = Engine()
    limiter = asyncio.Semaphore(concurrency)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await engine.start()
        log.info("pwflow engine ready (flows=%s, concurrency=%d)", flows_dir, concurrency)
        try:
            yield
        finally:
            await engine.close()

    api = FastAPI(title="pwflow", version="0.1.0", lifespan=lifespan)

    def resolve(req: RunRequest) -> Flow:
        try:
            if req.yaml is not None:
                flow = load_flow(req.yaml)
            else:
                path = _flow_path(flows_dir, req.flow)  # type: ignore[arg-type]
                flow = load_flow(path)
        except FlowLoadError as e:
            raise HTTPException(422, detail=str(e)) from e
        if req.headless is not None:
            flow.browser.headless = req.headless
        return flow

    async def execute(flow: Flow, req: RunRequest, run_id: str) -> RunRecord:
        record = runs[run_id]
        async with limiter:
            record.status = "running"
            try:
                result = await engine.run(flow, vars=req.vars, run_id=run_id)
            except Exception as e:  # noqa: BLE001 - never let a run kill the server
                log.exception("run %s crashed", run_id)
                record.status, record.error = "failed", f"{type(e).__name__}: {e}"
                return record
        payload = result.to_dict()
        record.status = result.status
        record.duration_ms = result.duration_ms
        record.error = result.error
        record.data = result.data
        record.artifacts = result.artifacts
        record.steps = payload["steps"]
        return record

    @api.post("/runs", response_model=RunRecord)
    async def create_run(req: RunRequest) -> RunRecord:
        flow = resolve(req)
        run_id = uuid.uuid4().hex[:12]
        runs[run_id] = RunRecord(
            run_id=run_id, flow=flow.name, status="queued", created_at=time.time()
        )
        if req.wait:
            return await execute(flow, req, run_id)
        asyncio.create_task(execute(flow, req, run_id))  # noqa: RUF006
        return runs[run_id]

    @api.get("/runs", response_model=list[RunRecord])
    async def list_runs(limit: int = 50) -> list[RunRecord]:
        return sorted(runs.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    @api.get("/runs/{run_id}", response_model=RunRecord)
    async def get_run(run_id: str) -> RunRecord:
        if run_id not in runs:
            raise HTTPException(404, detail=f"no run {run_id}")
        return runs[run_id]

    @api.get("/flows")
    async def list_flows() -> list[dict[str, Any]]:
        out = []
        for path in sorted(flows_dir.glob("*.y*ml")) if flows_dir.exists() else []:
            entry: dict[str, Any] = {"name": path.stem, "path": str(path)}
            try:
                flow = load_flow(path)
                entry |= {
                    "description": flow.description,
                    "vars": flow.vars,
                    "steps": len(flow.steps),
                    "valid": True,
                }
            except FlowLoadError as e:
                entry |= {"valid": False, "error": str(e)}
            out.append(entry)
        return out

    @api.post("/validate")
    async def validate(req: RunRequest) -> dict[str, Any]:
        flow = resolve(req)
        return {"valid": True, "name": flow.name, "steps": len(flow.steps)}

    @api.get("/actions")
    async def list_actions() -> dict[str, Any]:
        return {
            spec.name: {
                "doc": spec.doc,
                "aliases": list(spec.aliases),
                "control": spec.control,
                "shorthand": spec.shorthand,
                "params": spec.model.model_json_schema(),
            }
            for spec in canonical()
        }

    @api.get("/healthz")
    async def healthz() -> dict[str, Any]:
        active = sum(1 for r in runs.values() if r.status in ("queued", "running"))
        return {"ok": True, "active_runs": active, "total_runs": len(runs)}

    return api


def _flow_path(flows_dir: Path, name: str) -> Path:
    # A flow name is a filename, never a path — do not let a caller read /etc/passwd.
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, detail=f"bad flow name: {name!r}")
    for suffix in (".yaml", ".yml"):
        candidate = flows_dir / f"{name}{suffix}"
        if candidate.exists():
            return candidate
    raise HTTPException(404, detail=f"no flow named {name!r} in {flows_dir}")
