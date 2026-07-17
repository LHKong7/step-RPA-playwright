"""The in-process metrics registry and its Prometheus exposition."""

import math

import pytest

from pwflow.metrics import Registry, observe_run, observe_step


@pytest.fixture
def reg() -> Registry:
    return Registry()


def test_counter_accumulates_by_label(reg: Registry):
    c = reg.counter("hits_total", "Hits.", ("route",))
    c.inc(route="/a")
    c.inc(2, route="/a")
    c.inc(route="/b")
    assert c.value(route="/a") == 3
    assert c.value(route="/b") == 1
    assert c.value(route="/missing") == 0


def test_gauge_goes_up_and_down(reg: Registry):
    g = reg.gauge("inflight", "In flight.")
    g.inc()
    g.inc()
    g.dec()
    assert g.value() == 1
    g.set(5)
    assert g.value() == 5


def test_histogram_buckets_are_cumulative(reg: Registry):
    h = reg.histogram("dur", "Duration.", buckets=(0.1, 1.0))
    for v in (0.05, 0.2, 0.2, 5.0):
        h.observe(v)
    [(_, counts, total)] = h._samples()
    # buckets are (0.1, 1.0, inf); cumulative counts <= each bound
    assert counts == [1, 3, 4]
    assert total == pytest.approx(5.45)


def test_render_is_prometheus_text(reg: Registry):
    c = reg.counter("runs_total", "Runs.", ("status",))
    c.inc(status="ok")
    out = reg.render()
    assert "# HELP runs_total Runs." in out
    assert "# TYPE runs_total counter" in out
    assert 'runs_total{status="ok"} 1' in out
    assert out.endswith("\n")


def test_label_free_counter_renders_zero_when_empty(reg: Registry):
    reg.counter("empty_total", "Nothing yet.")
    assert "empty_total 0" in reg.render()


def test_histogram_render_has_bucket_sum_count(reg: Registry):
    h = reg.histogram("lat", "Latency.", ("op",), buckets=(1.0,))
    h.observe(0.5, op="read")
    h.observe(2.0, op="read")
    out = reg.render()
    assert 'lat_bucket{op="read",le="1"} 1' in out
    assert 'lat_bucket{op="read",le="+Inf"} 2' in out
    assert 'lat_sum{op="read"} 2.5' in out
    assert 'lat_count{op="read"} 2' in out


def test_label_values_are_escaped(reg: Registry):
    c = reg.counter("weird_total", "Weird.", ("name",))
    c.inc(name='a"b\\c')
    assert r'name="a\"b\\c"' in reg.render()


def test_reset_zeroes_samples_but_keeps_registration(reg: Registry):
    c = reg.counter("x_total", "X.", ("k",))
    c.inc(k="v")
    reg.reset()
    assert c.value(k="v") == 0
    # still registered — same object comes back, no double-register
    assert reg.counter("x_total", "X.", ("k",)) is c


def test_registration_is_idempotent(reg: Registry):
    a = reg.counter("dup_total", "First.")
    b = reg.counter("dup_total", "Second.")
    assert a is b


def test_inf_bucket_always_present(reg: Registry):
    h = reg.histogram("h", "H.", buckets=(1.0,))
    assert h.buckets[-1] == math.inf


def test_observe_helpers_hit_the_default_registry():
    from pwflow.metrics import (
        METRICS,
        RUN_DURATION,
        RUNS_TOTAL,
        STEP_RETRIES_TOTAL,
        STEPS_TOTAL,
    )

    METRICS.reset()
    observe_run("demo", "success", 1500)
    observe_step("click", "ok", 40, attempts=1)
    observe_step("click", "recovered", 120, attempts=3)

    assert RUNS_TOTAL.value(flow="demo", status="success") == 1
    assert STEPS_TOTAL.value(action="click", status="ok") == 1
    assert STEPS_TOTAL.value(action="click", status="recovered") == 1
    # 3 attempts == 2 retries
    assert STEP_RETRIES_TOTAL.value(action="click") == 2
    [(_, _, total)] = [s for s in RUN_DURATION._samples() if s[0] == (("flow", "demo"),)]
    assert total == pytest.approx(1.5)
