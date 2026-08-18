"""What one high-cardinality label costs.

`task_id` is the obvious thing to attach to a task metric and the wrong thing.
Every distinct attribute combination is a separate time series, so labelling by
task id makes series count track task count — unbounded, for ever, because task
ids are never reused.

This measures it rather than asserting it. Same workload, same metrics, one
flag flipped:

    python bench/cardinality.py
    python bench/cardinality.py --sizes 100,1000,10000,50000

Three things are measured, all of them real rather than modelled:

  series      exact count of exported time series
  scrape B    bytes of the actual Prometheus text exposition — what a scrape
              transfers, every scrape interval, for ever
  collect ms  wall time to gather metrics, which is the latency a scrape pays

The in-memory store is used deliberately: this is a question about the metrics
pipeline, and a database in the loop would only add noise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prometheus_client import CollectorRegistry, generate_latest
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider

from hapax import telemetry
from hapax.instrumented import InstrumentedTaskStore
from hapax.memory import InMemoryTaskStore
from hapax.task import TaskState


def measure(n_tasks: int, *, label_task_id: bool) -> dict:
    """Run `n_tasks` through an instrumented store and report what it costs.

    Each call gets a private CollectorRegistry, so the two configurations
    cannot contaminate each other's series counts — sharing the default
    registry would make the second run inherit the first run's series.
    """
    # Only the flag is toggled. Calling configure() here would try to install a
    # global MeterProvider on every iteration, which OpenTelemetry refuses after
    # the first — the metrics below come from the private provider built next.
    telemetry.set_label_task_id(label_task_id)

    registry = CollectorRegistry()
    reader = PrometheusMetricReader(registry=registry)
    provider = MeterProvider(metric_readers=[reader])
    instruments = telemetry.Instruments(meter=provider.get_meter("hapax"))

    store = InstrumentedTaskStore(InMemoryTaskStore(), instruments=instruments)

    # A full lifecycle per task: create, claim, complete. Three metrics touched
    # each time, which is what a real queue does.
    for i in range(n_tasks):
        task = store.create_task({"n": i})
        store.claim_task()
        store.update_task(task.id, TaskState.COMPLETED, result={"ok": True})

    started = time.perf_counter()
    payload = generate_latest(registry)
    collect_ms = (time.perf_counter() - started) * 1000

    # Count series the way Prometheus does: one per distinct name+labels line.
    series = sum(
        1
        for line in payload.decode().splitlines()
        if line and not line.startswith("#")
    )

    telemetry.set_label_task_id(False)
    return {
        "tasks": n_tasks,
        "label_task_id": label_task_id,
        "series": series,
        "scrape_bytes": len(payload),
        "collect_ms": round(collect_ms, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        default="100,1000,10000",
        help="comma-separated task counts to measure",
    )
    parser.add_argument("--json", type=Path, help="also write raw results here")
    args = parser.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]
    results = []

    print(f"{'tasks':>8}  {'task_id label':>13}  {'series':>9}  {'scrape B':>10}  {'collect ms':>10}")
    print("-" * 60)
    for n in sizes:
        for label in (False, True):
            row = measure(n, label_task_id=label)
            results.append(row)
            print(
                f"{row['tasks']:>8}  {str(label):>13}  {row['series']:>9,}  "
                f"{row['scrape_bytes']:>10,}  {row['collect_ms']:>10.2f}"
            )
        print("-" * 60)

    off = [r for r in results if not r["label_task_id"]]
    on = [r for r in results if r["label_task_id"]]

    print()
    print("Without task_id, series count is flat regardless of workload:")
    print(f"  {min(r['series'] for r in off)}–{max(r['series'] for r in off)} series "
          f"across {min(r['tasks'] for r in off):,}–{max(r['tasks'] for r in off):,} tasks")
    print()
    print("With task_id, it tracks task count:")
    for r in on:
        ratio = r["series"] / r["tasks"]
        print(f"  {r['tasks']:>7,} tasks -> {r['series']:>8,} series  ({ratio:.1f} per task)")

    biggest_on = max(on, key=lambda r: r["tasks"])
    biggest_off = max(off, key=lambda r: r["tasks"])
    print()
    print(
        f"At {biggest_on['tasks']:,} tasks the scrape payload goes from "
        f"{biggest_off['scrape_bytes']:,} B to {biggest_on['scrape_bytes']:,} B "
        f"({biggest_on['scrape_bytes'] / max(biggest_off['scrape_bytes'], 1):.0f}x), "
        f"and collection from {biggest_off['collect_ms']:.2f} ms to "
        f"{biggest_on['collect_ms']:.2f} ms."
    )
    print()
    print("That payload is transferred every scrape interval, indefinitely, and")
    print("task ids are never reused — so the 'with' column has no ceiling.")

    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
