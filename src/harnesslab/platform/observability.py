from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Counter:
    name: str
    description: str = ""
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def inc(self, amount: float = 1.0) -> float:
        if amount < 0:
            raise ValueError("counter increments cannot be negative")
        self.value += amount
        return self.value


@dataclass
class Gauge:
    name: str
    description: str = ""
    value: float = 0.0
    labels: dict[str, str] = field(default_factory=dict)

    def set(self, value: float) -> float:
        self.value = value
        return self.value


@dataclass
class Histogram:
    name: str
    description: str = ""
    buckets: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0)
    labels: dict[str, str] = field(default_factory=dict)
    count: int = 0
    total: float = 0.0
    bucket_counts: dict[float, int] = field(default_factory=dict)

    def observe(self, value: float) -> None:
        if value < 0:
            raise ValueError("histogram observations cannot be negative")
        self.count += 1
        self.total += value
        for bucket in self.buckets:
            if value <= bucket:
                self.bucket_counts[bucket] = self.bucket_counts.get(bucket, 0) + 1


@dataclass(frozen=True)
class SpanSnapshot:
    name: str
    duration_seconds: float
    attributes: dict[str, str]
    error: str | None = None


class Span:
    def __init__(self, name: str, *, attributes: dict[str, str] | None = None) -> None:
        self.name = name
        self.attributes = dict(attributes or {})
        self.started_at = time.perf_counter()
        self.error: str | None = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = str(value)

    def record_exception(self, error: BaseException) -> None:
        self.error = f"{type(error).__name__}: {error}"

    def finish(self) -> SpanSnapshot:
        return SpanSnapshot(
            name=self.name,
            duration_seconds=time.perf_counter() - self.started_at,
            attributes=dict(self.attributes),
            error=self.error,
        )


class MetricRegistry:
    """Bounded, dependency-free metrics registry for adapter and runtime instrumentation."""

    def __init__(self, *, max_metrics: int = 2_000, max_spans: int = 5_000) -> None:
        if max_metrics < 1 or max_spans < 1:
            raise ValueError("metric limits must be positive")
        self.max_metrics = max_metrics
        self.max_spans = max_spans
        self._metrics: dict[tuple[str, str, tuple[tuple[str, str], ...]], object] = {}
        self._spans: list[SpanSnapshot] = []
        self._lock = threading.RLock()

    def counter(
        self,
        name: str,
        *,
        description: str = "",
        labels: dict[str, str] | None = None,
    ) -> Counter:
        return self._get_or_create(
            "counter",
            name,
            labels or {},
            lambda: Counter(name, description, labels=labels or {}),
        )

    def gauge(
        self,
        name: str,
        *,
        description: str = "",
        labels: dict[str, str] | None = None,
    ) -> Gauge:
        return self._get_or_create(
            "gauge",
            name,
            labels or {},
            lambda: Gauge(name, description, labels=labels or {}),
        )

    def histogram(
        self,
        name: str,
        *,
        description: str = "",
        labels: dict[str, str] | None = None,
    ) -> Histogram:
        return self._get_or_create(
            "histogram",
            name,
            labels or {},
            lambda: Histogram(name, description, labels=labels or {}),
        )

    def record_span(self, span: Span | SpanSnapshot) -> SpanSnapshot:
        snapshot = span if isinstance(span, SpanSnapshot) else span.finish()
        with self._lock:
            self._spans.append(snapshot)
            if len(self._spans) > self.max_spans:
                del self._spans[: len(self._spans) - self.max_spans]
        return snapshot

    @contextmanager
    def trace(self, name: str, *, attributes: dict[str, str] | None = None) -> Iterator[Span]:
        span = Span(name, attributes=attributes)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            raise
        finally:
            self.record_span(span)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            metrics = {
                f"{kind}:{name}:{dict(labels)}": _metric_snapshot(metric)
                for (kind, name, labels), metric in self._metrics.items()
            }
            return {
                "metrics": metrics,
                "spans": [span.__dict__.copy() for span in self._spans],
            }

    def _get_or_create(self, kind: str, name: str, labels: dict[str, str], factory):
        key = (kind, name, tuple(sorted(labels.items())))
        with self._lock:
            metric = self._metrics.get(key)
            if metric is None:
                if len(self._metrics) >= self.max_metrics:
                    raise RuntimeError("metric cardinality limit exceeded")
                metric = factory()
                self._metrics[key] = metric
            return metric


def _metric_snapshot(metric: object) -> dict[str, object]:
    if isinstance(metric, Histogram):
        return {
            "type": "histogram",
            "count": metric.count,
            "total": metric.total,
            "buckets": dict(metric.bucket_counts),
            "labels": dict(metric.labels),
        }
    if isinstance(metric, Counter):
        return {"type": "counter", "value": metric.value, "labels": dict(metric.labels)}
    if isinstance(metric, Gauge):
        return {"type": "gauge", "value": metric.value, "labels": dict(metric.labels)}
    raise TypeError(f"unsupported metric: {type(metric).__name__}")
