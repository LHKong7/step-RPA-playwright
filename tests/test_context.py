"""RunContext template scope."""

from pwflow.context import RunContext
from pwflow.loader import load_flow


def _ctx(**vars) -> RunContext:
    flow = load_flow("name: t\nsteps:\n  - goto: https://x.com\n")
    # page/browser_context are unused by scope(); _page_url tolerates a None page.
    return RunContext(flow, page=None, browser_context=None, overrides=vars)


def test_env_is_snapshotted_once_not_reread_each_render(monkeypatch):
    monkeypatch.setenv("PWFLOW_SNAP", "before")
    ctx = _ctx()
    assert ctx.scope()["env"]["PWFLOW_SNAP"] == "before"

    # The snapshot is taken at construction; a later mutation must not leak into the run,
    # and — the point of the optimization — scope() must not re-read os.environ.
    monkeypatch.setenv("PWFLOW_SNAP", "after")
    assert ctx.scope()["env"]["PWFLOW_SNAP"] == "before"


def test_scope_exposes_live_vars_and_data():
    ctx = _ctx(pages=3)
    assert ctx.scope()["vars"]["pages"] == 3
    ctx.put_data("rows", [1, 2])
    # data is a live reference, so a later render sees the new records without a rebuild.
    assert ctx.scope()["data"]["rows"] == [1, 2]


def test_loop_locals_shadow_outer_scope():
    ctx = _ctx()
    with ctx.locals(item="x", index=0):
        s = ctx.scope()
        assert s["item"] == "x" and s["index"] == 0
    assert "item" not in ctx.scope()  # popped when the block exits
