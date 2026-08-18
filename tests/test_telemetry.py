"""The instrumentation layer, and the cardinality rule it exists to enforce.

These tests never touch Postgres. The wrapper is backend-agnostic by
construction — that is the point of decorating the Protocol rather than editing
a backend — so the in-memory store exercises every path.
"""

from __future__ import annotations

import pytest

from hapax import telemetry
from hapax.instrumented import InstrumentedTaskStore
from hapax.memory import InMemoryTaskStore
from hapax.store import TaskStore
from hapax.task import TaskState

pytest.importorskip("opentelemetry", reason="metrics assertions need the otel extra")

from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402


@pytest.fixture
def reader_and_store():
    """A store wired to a private MeterProvider.

    Deliberately not going through `telemetry.configure()`: OpenTelemetry's
    global provider can only be set once per process, so tests that configured
    it would pass alone and fail as a suite.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    instruments = telemetry.Instruments(meter=provider.get_meter("test"))
    store = InstrumentedTaskStore(InMemoryTaskStore(), instruments=instruments)
    yield reader, store
    telemetry.reset_for_testing()


def _points(reader: InMemoryMetricReader, name: str) -> list:
    """Every data point recorded for one metric, across resources/scopes."""
    data = reader.get_metrics_data()
    if data is None:
        return []
    out = []
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    out.extend(metric.data.data_points)
    return out


def test_the_wrapper_is_still_a_taskstore():
    """`TaskStore` is a runtime_checkable Protocol, so this is the actual
    contract check — a wrapper that dropped a method would fail here rather
    than at some call site later."""
    store = InstrumentedTaskStore(InMemoryTaskStore())
    assert isinstance(store, TaskStore)


def test_everything_works_with_no_opentelemetry_configured():
    """The core install has no dependencies, so the uninstrumented path is the
    common one. It must be fully functional, not merely non-crashing."""
    telemetry.reset_for_testing()
    store = InstrumentedTaskStore(InMemoryTaskStore())

    task = store.create_task({"charge": 100}, idempotency_key="k1")
    claimed = store.claim_task()
    assert claimed is not None
    assert claimed.id == task.id

    done = store.update_task(task.id, TaskState.COMPLETED, result={"ok": True})
    assert done.state is TaskState.COMPLETED


def test_uninstrumented_methods_are_forwarded():
    """The wrapper must not have to grow a method every time the store does."""
    inner = InMemoryTaskStore()
    store = InstrumentedTaskStore(inner)
    store.create_task({"a": 1})

    # list_tasks and count_claimable are never named in instrumented.py.
    assert len(store.list_tasks()) == 1
    assert store.count_claimable() == 1


def test_creates_and_claims_are_counted(reader_and_store):
    reader, store = reader_and_store
    store.create_task({"n": 1})
    store.create_task({"n": 2})
    store.claim_task()

    assert sum(p.value for p in _points(reader, "hapax.tasks.created")) == 2
    assert sum(p.value for p in _points(reader, "hapax.tasks.claimed")) == 1


def test_a_claim_that_finds_nothing_is_still_timed(reader_and_store):
    """An empty queue is the case where claim latency matters most — it is the
    poll that runs constantly. Timing only successful claims would leave the
    hot path unmeasured."""
    reader, store = reader_and_store
    assert store.claim_task() is None

    points = _points(reader, "hapax.claim.duration")
    assert points, "a missed claim recorded no duration"
    assert sum(p.count for p in points) == 1


def test_a_first_claim_is_not_a_recovery(reader_and_store):
    reader, store = reader_and_store
    store.create_task({"n": 1})
    store.claim_task()

    assert sum(p.value for p in _points(reader, "hapax.tasks.recovered")) == 0


def test_reclaiming_a_lapsed_task_counts_as_recovery(reader_and_store):
    """Nothing reports the crash. The second claim increments `attempts`, and
    that increment is the entire evidence that a previous holder died."""
    reader, store = reader_and_store
    store.create_task({"n": 1})

    first = store.claim_task(lease_seconds=0)
    assert first is not None and first.attempts == 1

    # Lease of zero seconds is already expired, so the task is claimable again
    # without waiting — the same state a killed worker leaves behind.
    second = store.claim_task()
    assert second is not None and second.attempts == 2

    assert sum(p.value for p in _points(reader, "hapax.tasks.recovered")) == 1


def test_transitions_are_labelled_by_destination_state(reader_and_store):
    reader, store = reader_and_store
    task = store.create_task({"n": 1})
    store.update_task(task.id, TaskState.COMPLETED, result={"ok": True})

    points = _points(reader, "hapax.tasks.transitions")
    assert points
    assert any(p.attributes.get("to_state") == "completed" for p in points)


# --- the cardinality rule ----------------------------------------------------


def test_task_id_is_not_a_metric_attribute_by_default(reader_and_store):
    """The rule the whole telemetry module is built around.

    One series per task is unbounded growth: a queue that has handled a million
    tasks would produce a million series. task_id belongs on spans, which are
    retrieved by id rather than aggregated across."""
    reader, store = reader_and_store
    for i in range(5):
        store.create_task({"n": i})

    points = _points(reader, "hapax.tasks.created")
    assert len(points) == 1, f"{len(points)} series from 5 tasks — task_id leaked in"
    assert "task_id" not in (points[0].attributes or {})


def test_opting_into_task_id_produces_one_series_per_task(reader_and_store):
    """The failure mode, demonstrated rather than asserted.

    This is what bench/cardinality.py measures at scale: turning the flag on
    makes series count track task count exactly."""
    reader, store = reader_and_store
    telemetry.set_label_task_id(True)

    for i in range(5):
        store.create_task({"n": i})

    points = _points(reader, "hapax.tasks.created")
    assert len(points) == 5
    assert all("task_id" in (p.attributes or {}) for p in points)


def test_attrs_drops_task_id_unless_opted_in():
    telemetry.reset_for_testing()
    assert telemetry.attrs(task_id="task_abc") == {}

    telemetry.set_label_task_id(True)
    assert telemetry.attrs(task_id="task_abc") == {"task_id": "task_abc"}
    telemetry.reset_for_testing()


def test_attrs_drops_none_valued_extras():
    """None attributes would render as the string "None" in Prometheus, which
    is a value that looks real and is not."""
    telemetry.reset_for_testing()
    assert telemetry.attrs(to_state=None, with_effect=True) == {"with_effect": True}
