"""FileStore: bounded memory, write-through durability, restart recovery."""

from pathlib import Path

from pwflow.server.store import FileStore, RunRecord


def _rec(run_id: str, status: str = "success", created: float = 0.0) -> RunRecord:
    return RunRecord(run_id=run_id, flow="t", status=status, created_at=created)


def test_save_and_get_roundtrip(tmp_path: Path):
    store = FileStore(tmp_path)
    store.save(_rec("a"))
    rec = store.get("a")
    assert rec is not None and rec.run_id == "a"


def test_data_survives_the_json_roundtrip_on_disk(tmp_path: Path):
    store = FileStore(tmp_path, max_memory=0)  # force every read to come from disk
    rec = _rec("a")
    rec.data = {"rows": [{"n": 1}, {"n": 2}]}
    store.save(rec)
    back = store.get("a")
    assert back is not None and back.data == {"rows": [{"n": 1}, {"n": 2}]}


def test_evicted_record_is_still_readable_from_disk(tmp_path: Path):
    store = FileStore(tmp_path, max_memory=2)
    for i in range(3):
        store.save(_rec(f"r{i}", created=float(i)))
    # r0 was evicted from the memory cache...
    assert "r0" not in store._mem
    # ...but the file remains and get() reads it back.
    assert store.get("r0") is not None


def test_in_flight_records_are_never_evicted(tmp_path: Path):
    store = FileStore(tmp_path, max_memory=1)
    store.save(_rec("running", status="running", created=0.0))
    store.save(_rec("done1", status="success", created=1.0))
    store.save(_rec("done2", status="success", created=2.0))
    # the running record must still be resident — it is about to be updated again
    assert "running" in store._mem


def test_list_returns_recent_first(tmp_path: Path):
    store = FileStore(tmp_path)
    for i in range(5):
        store.save(_rec(f"r{i}", created=float(i)))
    ids = [r.run_id for r in store.list(limit=3)]
    assert ids == ["r4", "r3", "r2"]


def test_recover_marks_in_flight_runs_interrupted(tmp_path: Path):
    # A process wrote these, then died mid-run.
    dying = FileStore(tmp_path)
    dying.save(_rec("was_running", status="running", created=1.0))
    dying.save(_rec("finished", status="success", created=2.0))

    # A fresh process starts up against the same directory.
    fresh = FileStore(tmp_path)
    interrupted = fresh.recover()
    assert interrupted == 1
    assert fresh.get("was_running").status == "interrupted"
    assert fresh.get("was_running").error  # carries a reason
    assert fresh.get("finished").status == "success"  # terminal runs are left alone


def test_atomic_write_leaves_no_tmp_file(tmp_path: Path):
    store = FileStore(tmp_path)
    store.save(_rec("a"))
    assert (tmp_path / "a.json").exists()
    assert not list(tmp_path.glob("*.tmp"))
