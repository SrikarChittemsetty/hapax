"""Metrics and traces for Hapax.

Hapax has no required dependencies, and that stays true: OpenTelemetry is an
optional extra (`pip install 'hapax[otel]'`). With it absent every call here
becomes a no-op, so instrumented code paths run unchanged in a plain install and
the test suite never needs the SDK.

The interesting design decision is in `label_task_id`. Read the note on
CARDINALITY below before turning it on.

Usage:

    from hapax import telemetry
    telemetry.configure(prometheus_port=9464)
    store = InstrumentedTaskStore(PostgresTaskStore(conninfo))

Then scrape http://localhost:9464/metrics.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

try:  # pragma: no cover - exercised by whether the extra is installed
    from opentelemetry import metrics as _otel_metrics
    from opentelemetry import trace as _otel_trace

    _HAVE_OTEL = True
except ImportError:  # pragma: no cover
    _HAVE_OTEL = False


INSTRUMENTATION_NAME = "hapax"

# ---------------------------------------------------------------------------
# CARDINALITY
#
# Every distinct combination of metric name and attribute values is a separate
# time series. Attaching `task_id` to a metric therefore creates one series per
# task, for ever — a queue that has handled a million tasks produces a million
# series, and the store behind it falls over long before that.
#
# So task_id is *deliberately not* a metric attribute by default. It belongs on
# spans, where high-cardinality identifiers are free: a trace is looked up by id
# rather than aggregated across, so one span per task costs one span.
#
# The flag exists so the failure can be demonstrated rather than asserted; see
# bench/cardinality.py, which measures the series count both ways.
# ---------------------------------------------------------------------------

_state: dict[str, Any] = {"configured": False, "label_task_id": False}


def configure(
    *,
    prometheus_port: int | None = None,
    service_name: str = "hapax",
    label_task_id: bool = False,
    console_traces: bool = False,
) -> bool:
    """Set up metrics/tracing. Returns False if OpenTelemetry isn't installed.

    Safe to call more than once; later calls only update `label_task_id`, since
    swapping a configured MeterProvider out from under live instruments would
    silently orphan them.
    """
    _state["label_task_id"] = label_task_id

    if not _HAVE_OTEL:
        return False

    if _state["configured"]:
        return True

    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    resource = Resource.create({"service.name": service_name})

    readers = []
    if prometheus_port is not None:
        from prometheus_client import start_http_server
        from opentelemetry.exporter.prometheus import PrometheusMetricReader

        start_http_server(prometheus_port)
        readers.append(PrometheusMetricReader())

    _otel_metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    tracer_provider = TracerProvider(resource=resource)
    if console_traces:
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

        tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    _otel_trace.set_tracer_provider(tracer_provider)

    _state["configured"] = True
    return True


def set_label_task_id(enabled: bool) -> None:
    """Toggle the cardinality rule on its own.

    Separate from `configure()` because OpenTelemetry refuses to replace an
    already-set global provider — it logs "Overriding of current MeterProvider
    is not allowed" and carries on with the old one. Anything that flips this
    flag repeatedly (bench/cardinality.py, the tests) needs a way to do so
    without pretending to reconfigure the world.
    """
    _state["label_task_id"] = enabled


def is_enabled() -> bool:
    return _HAVE_OTEL and bool(_state["configured"])


def labels_task_id() -> bool:
    return bool(_state["label_task_id"])


@dataclass
class _NoopInstrument:
    """Stands in for a counter/histogram when the SDK is absent."""

    def add(self, amount: int, attributes: dict[str, Any] | None = None) -> None:
        return None

    def record(self, amount: float, attributes: dict[str, Any] | None = None) -> None:
        return None


class Instruments:
    """The metric set, created once and reused.

    Names follow the OpenTelemetry convention of dotted namespaces and units in
    the name where they are not obvious, so a Prometheus exporter renders them
    as `hapax_tasks_created_total` and friends without further mapping.
    """

    def __init__(self, meter: Any = None) -> None:
        """`meter` is injectable so tests can attach an in-memory reader.

        OpenTelemetry's global MeterProvider can only be set once per process,
        so a test that went through `configure()` could never assert on a second
        set of metrics. Passing a meter sidesteps the global entirely.
        """
        if meter is None and not is_enabled():
            noop = _NoopInstrument()
            self.created = noop
            self.claimed = noop
            self.recovered = noop
            self.transitions = noop
            self.claim_duration = noop
            self.abandoned = noop
            return

        if meter is None:
            meter = _otel_metrics.get_meter(INSTRUMENTATION_NAME)
        self.created = meter.create_counter(
            "hapax.tasks.created", unit="1", description="Tasks created."
        )
        self.claimed = meter.create_counter(
            "hapax.tasks.claimed", unit="1", description="Successful claims."
        )
        self.recovered = meter.create_counter(
            "hapax.tasks.recovered",
            unit="1",
            description="Claims of a task whose previous holder died and let its lease lapse.",
        )
        self.transitions = meter.create_counter(
            "hapax.tasks.transitions", unit="1", description="State transitions applied."
        )
        self.claim_duration = meter.create_histogram(
            "hapax.claim.duration",
            unit="s",
            description="Wall time of a claim_task call, including the miss case.",
        )
        self.abandoned = meter.create_counter(
            "hapax.tasks.abandoned",
            unit="1",
            description="Tasks past max_attempts and no longer dispatched.",
        )


_instruments: Instruments | None = None


def instruments() -> Instruments:
    global _instruments
    if _instruments is None:
        _instruments = Instruments()
    return _instruments


def reset_for_testing() -> None:
    """Drop cached instruments so a test can reconfigure. Not for production."""
    global _instruments
    _instruments = None
    _state["configured"] = False
    _state["label_task_id"] = False


def attrs(task_id: str | None = None, **extra: Any) -> dict[str, Any]:
    """Build metric attributes, honouring the cardinality rule.

    `task_id` is dropped unless explicitly opted into, so the common path cannot
    accidentally create per-task series.
    """
    out: dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
    if task_id is not None and labels_task_id():
        out["task_id"] = task_id
    return out


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start a span, or do nothing at all if tracing isn't configured.

    Unlike metrics, spans carry `task_id` freely — traces are retrieved by id
    rather than aggregated over, so a unique attribute costs one span rather
    than one time series.
    """
    if not is_enabled():
        yield None
        return

    tracer = _otel_trace.get_tracer(INSTRUMENTATION_NAME)
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


@contextmanager
def timed(histogram: Any, attributes: dict[str, Any]) -> Iterator[None]:
    """Record elapsed seconds into `histogram`, including on the failure path.

    Timing only successful calls would hide exactly the case worth seeing: a
    claim that got slow because it was contending, then raised.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        histogram.record(time.perf_counter() - started, attributes)
