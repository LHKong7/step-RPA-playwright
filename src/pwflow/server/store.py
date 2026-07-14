"""Where run records live.

The naive ``dict[run_id, RunRecord]`` had two failure modes for a long-running service:
it grew without bound (every run's full ``data`` kept forever → OOM), and a restart lost
everything. ``FileStore`` fixes both — a bounded in-memory cache in front of write-through
JSON files — while keeping the same tiny surface the API calls.

Honest boundary: this is best-effort single-process durability. Terminal runs survive a
restart and stay queryable by id; runs that were mid-flight when the process died cannot
resume (their browser is gone) and are marked ``interrupted`` on the next startup. Real
at-least-once execution needs an external job queue — out of scope here.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

log = logging.getLogger("pwflow")

Status = Literal["queued", "running", "success", "failed", "interrupted"]
_TERMINAL = ("success", "failed", "interrupted")


class RunRecord(BaseModel):
    run_id: str
    flow: str
    status: Status
    created_at: float
    duration_ms: int | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    data: dict[str, Any] | None = None
    artifacts: list[str] | None = None
    steps: list[dict[str, Any]] | None = None


class FileStore:
    """Bounded in-memory cache + write-through JSON files under ``runs_dir``.

    ``max_memory`` caps how many full records (with their ``data``) stay resident; older
    terminal records are evicted from memory but remain on disk and are re-read on demand.
    In-flight records are never evicted — they are about to be updated again.
    """

    def __init__(self, runs_dir: Path, max_memory: int = 200) -> None:
        self.runs_dir = Path(runs_dir)
        self.max_memory = max_memory
        self._mem: OrderedDict[str, RunRecord] = OrderedDict()

    # -- lifecycle ---------------------------------------------------------

    def recover(self) -> int:
        """Load persisted records; mark anything left mid-flight as interrupted.

        Returns the number of interrupted runs found, for a startup log line.
        """
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        records: list[RunRecord] = []
        for path in self.runs_dir.glob("*.json"):
            try:
                records.append(RunRecord.model_validate_json(path.read_text()))
            except Exception as e:  # noqa: BLE001 - a corrupt file must not stop startup
                log.warning("skipping unreadable run file %s: %s", path.name, e)

        interrupted = 0
        for rec in records:
            if rec.status in ("queued", "running"):
                rec.status = "interrupted"
                rec.error = rec.error or "process restarted while this run was in flight"
                self._write(rec)
                interrupted += 1

        # Keep the most recent records warm in memory, up to the cap.
        for rec in sorted(records, key=lambda r: r.created_at)[-self.max_memory :]:
            self._mem[rec.run_id] = rec
        return interrupted

    # -- reads/writes ------------------------------------------------------

    def save(self, rec: RunRecord) -> None:
        self._mem[rec.run_id] = rec
        self._mem.move_to_end(rec.run_id)
        self._write(rec)
        self._evict()

    def get(self, run_id: str) -> RunRecord | None:
        rec = self._mem.get(run_id)
        if rec is not None:
            return rec
        path = self._path(run_id)
        if not path.exists():
            return None
        try:
            return RunRecord.model_validate_json(path.read_text())
        except Exception as e:  # noqa: BLE001
            log.warning("run file %s is unreadable: %s", path.name, e)
            return None

    def list(self, limit: int = 50) -> list[RunRecord]:
        # Recent runs come from the warm cache; older ones stay on disk, fetchable by id.
        return sorted(self._mem.values(), key=lambda r: r.created_at, reverse=True)[:limit]

    def active_count(self) -> int:
        return sum(1 for r in self._mem.values() if r.status in ("queued", "running"))

    # -- internals ---------------------------------------------------------

    def _path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def _write(self, rec: RunRecord) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(rec.run_id).with_suffix(".json.tmp")
        tmp.write_text(rec.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, self._path(rec.run_id))  # atomic: no half-written file is ever read

    def _evict(self) -> None:
        # Drop oldest *terminal* records from memory; disk keeps them. Never evict an
        # in-flight run (it is about to be saved again). In-flight count <= concurrency,
        # which is far below max_memory, so this can only skip a handful.
        checked = 0
        while len(self._mem) > self.max_memory and checked < len(self._mem):
            run_id, rec = next(iter(self._mem.items()))
            if rec.status not in _TERMINAL:
                self._mem.move_to_end(run_id)
                checked += 1
                continue
            self._mem.popitem(last=False)
