"""In-process metrics — counters, gauges, histograms, and Prometheus exposition.

Deliberately dependency-free. A scraper service wants to answer "how many runs
failed in the last hour, and how slow is p95" without pulling `prometheus_client`
and its process-registry global state into a library that also runs as a one-shot
CLI. So this is a few hundred lines of the parts that matter: the three metric
types, labels, and the text format a Prometheus/OpenMetrics scraper reads.

The registry is a module-level singleton (`METRICS`), because metrics are
inherently a global side channel — the engine records into the same registry the
HTTP `/metrics` endpoint reads from, with no plumbing between them. Recording is
cheap and always on; nothing has to opt in. Tests call `METRICS.reset()`.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable

# Buckets for durations measured in seconds. Covers a fast DOM click (tens of ms)
# up to a run that grinds against `max_duration` (minutes). `inf` is implied and
# appended automatically.
DURATION_BUCKETS: tuple[float, ...] = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0,
)

_LabelKey = tuple[tuple[str, str], ...]


def _labelkey(labelnames: tuple[str, ...], labels: dict[str, str]) -> _LabelKey:
    """Freeze a label set into a hashable, order-stable key.

    Missing labels default to "" rather than raising: a metric that is sometimes
    labelled and sometimes not is a nuisance to query but never a crash mid-run.
    """
    return tuple((name, str(labels.get(name, ""))) for name in labelnames)


class _Metric:
    def __init__(self, name: str, help: str, labelnames: Iterable[str] = ()) -> None:
        self.name = name
        self.help = help
        self.labelnames = tuple(labelnames)
        self._lock = threading.Lock()


class Counter(_Metric):
    """Monotonically increasing value (runs completed, steps failed, retries)."""

    def __init__(self, name: str, help: str, labelnames: Iterable[str] = ()) -> None:
        super().__init__(name, help, labelnames)
        self._values: dict[_LabelKey, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _labelkey(self.labelnames, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: str) -> float:
        return self._values.get(_labelkey(self.labelnames, labels), 0.0)

    def _samples(self) -> list[tuple[_LabelKey, float]]:
        with self._lock:
            return list(self._values.items())


class Gauge(_Metric):
    """A value that goes up and down (runs currently in flight)."""

    def __init__(self, name: str, help: str, labelnames: Iterable[str] = ()) -> None:
        super().__init__(name, help, labelnames)
        self._values: dict[_LabelKey, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _labelkey(self.labelnames, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def set(self, value: float, **labels: str) -> None:
        key = _labelkey(self.labelnames, labels)
        with self._lock:
            self._values[key] = value

    def value(self, **labels: str) -> float:
        return self._values.get(_labelkey(self.labelnames, labels), 0.0)

    def _samples(self) -> list[tuple[_LabelKey, float]]:
        with self._lock:
            return list(self._values.items())


class Histogram(_Metric):
    """Cumulative bucket counts plus running sum/count — the shape Prometheus wants.

    Each observation lands in every bucket whose upper bound it is ``<=``, so the
    exported ``_bucket`` series are already cumulative. p50/p95 are then a scraper
    query (``histogram_quantile``), not something computed here.
    """

    def __init__(
        self,
        name: str,
        help: str,
        labelnames: Iterable[str] = (),
        buckets: tuple[float, ...] = DURATION_BUCKETS,
    ) -> None:
        super().__init__(name, help, labelnames)
        self.buckets = tuple(sorted(buckets)) + (math.inf,)
        self._counts: dict[_LabelKey, list[int]] = {}
        self._sum: dict[_LabelKey, float] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = _labelkey(self.labelnames, labels)
        with self._lock:
            counts = self._counts.get(key)
            if counts is None:
                counts = self._counts[key] = [0] * len(self.buckets)
                self._sum[key] = 0.0
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[i] += 1
            self._sum[key] += value

    def _samples(self) -> list[tuple[_LabelKey, list[int], float]]:
        with self._lock:
            return [(key, list(counts), self._sum[key]) for key, counts in self._counts.items()]


class Registry:
    """Holds the named metrics and renders them in Prometheus text format."""

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def _register(self, metric: _Metric) -> _Metric:
        with self._lock:
            existing = self._metrics.get(metric.name)
            if existing is not None:
                return existing  # idempotent: importing a module twice must not double-register
            self._metrics[metric.name] = metric
            return metric

    def counter(self, name: str, help: str, labelnames: Iterable[str] = ()) -> Counter:
        return self._register(Counter(name, help, labelnames))  # type: ignore[return-value]

    def gauge(self, name: str, help: str, labelnames: Iterable[str] = ()) -> Gauge:
        return self._register(Gauge(name, help, labelnames))  # type: ignore[return-value]

    def histogram(
        self, name: str, help: str, labelnames: Iterable[str] = (),
        buckets: tuple[float, ...] = DURATION_BUCKETS,
    ) -> Histogram:
        return self._register(Histogram(name, help, labelnames, buckets))  # type: ignore[return-value]

    def reset(self) -> None:
        """Zero every metric in place (for tests). Keeps registrations, drops samples."""
        with self._lock:
            for metric in self._metrics.values():
                if isinstance(metric, (Counter, Gauge)):
                    metric._values.clear()
                elif isinstance(metric, Histogram):
                    metric._counts.clear()
                    metric._sum.clear()

    def render(self) -> str:
        """Prometheus text exposition format (also valid OpenMetrics-ish)."""
        lines: list[str] = []
        with self._lock:
            metrics = list(self._metrics.values())
        for metric in metrics:
            if isinstance(metric, Counter):
                lines += _render_flat(metric, "counter", metric._samples())
            elif isinstance(metric, Gauge):
                lines += _render_flat(metric, "gauge", metric._samples())
            elif isinstance(metric, Histogram):
                lines += _render_histogram(metric)
        return "\n".join(lines) + "\n" if lines else ""


def _fmt_labels(labelkey: _LabelKey, extra: tuple[tuple[str, str], ...] = ()) -> str:
    pairs = tuple(labelkey) + extra
    if not pairs:
        return ""
    inner = ",".join(f'{name}="{_escape(value)}"' for name, value in pairs)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_number(value: float) -> str:
    if value == math.inf:
        return "+Inf"
    if value == int(value):
        return str(int(value))
    return repr(value)


def _render_flat(metric: _Metric, kind: str, samples: list[tuple[_LabelKey, float]]) -> list[str]:
    out = [f"# HELP {metric.name} {metric.help}", f"# TYPE {metric.name} {kind}"]
    if not samples:
        # Emit a zero sample for label-free metrics so a fresh scrape shows the series.
        if not metric.labelnames:
            out.append(f"{metric.name} 0")
        return out
    for labelkey, value in sorted(samples):
        out.append(f"{metric.name}{_fmt_labels(labelkey)} {_fmt_number(value)}")
    return out


def _render_histogram(metric: Histogram) -> list[str]:
    out = [f"# HELP {metric.name} {metric.help}", f"# TYPE {metric.name} histogram"]
    for labelkey, counts, total in sorted(metric._samples()):
        # counts[i] is already the number of observations <= buckets[i] (see observe),
        # i.e. cumulative, which is exactly what a `_bucket` series must report.
        for bound, count in zip(metric.buckets, counts, strict=True):
            le = ("le", _fmt_number(bound))
            out.append(f"{metric.name}_bucket{_fmt_labels(labelkey, (le,))} {count}")
        out.append(f"{metric.name}_sum{_fmt_labels(labelkey)} {_fmt_number(total)}")
        out.append(f"{metric.name}_count{_fmt_labels(labelkey)} {counts[-1]}")
    return out


# -- the default registry and pwflow's metrics ----------------------------------

METRICS = Registry()

RUNS_TOTAL = METRICS.counter(
    "pwflow_runs_total", "Flow runs that reached a terminal state, by flow and status.",
    ("flow", "status"),
)
RUN_DURATION = METRICS.histogram(
    "pwflow_run_duration_seconds", "Wall-clock duration of a run, by flow.", ("flow",),
)
ACTIVE_RUNS = METRICS.gauge(
    "pwflow_active_runs", "Runs currently executing.",
)
STEPS_TOTAL = METRICS.counter(
    "pwflow_steps_total", "Steps executed, by action and outcome.", ("action", "status"),
)
STEP_DURATION = METRICS.histogram(
    "pwflow_step_duration_seconds", "Wall-clock duration of a step, by action.", ("action",),
)
STEP_RETRIES_TOTAL = METRICS.counter(
    "pwflow_step_retries_total", "Retry attempts made, by action.", ("action",),
)


def observe_step(action: str, status: str, duration_ms: int, attempts: int = 1) -> None:
    """Record one step's outcome. Called from the executor at every terminal path."""
    STEPS_TOTAL.inc(action=action, status=status)
    STEP_DURATION.observe(duration_ms / 1000.0, action=action)
    if attempts > 1:
        STEP_RETRIES_TOTAL.inc(attempts - 1, action=action)


def observe_run(flow: str, status: str, duration_ms: int) -> None:
    """Record one run's outcome. Called from the engine after teardown."""
    RUNS_TOTAL.inc(flow=flow, status=status)
    RUN_DURATION.observe(duration_ms / 1000.0, flow=flow)
