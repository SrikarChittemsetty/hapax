"""Benchmark: what does durability cost?

Measures per-operation latency (p50/p95/p99) and throughput for the core store
operations, comparing the in-memory reference backend against the durable
Postgres backend. The in-memory numbers are the floor (pure dict operations, no
I/O); the gap to Postgres is the price of surviving a crash — which is the
number the whole project is really about.

Methodology (kept deliberately simple and honest):
  * A warmup phase runs first and is discarded, so JIT-less Python import costs,
    connection setup, and OS page-cache warmup don't pollute the measurement.
  * Each operation is timed individually with time.perf_counter (a monotonic
    high-resolution clock), collected into a list, and reduced to percentiles.
  * Percentiles are reported because averages hide tail latency, and tail
    latency is what actually hurts in a durable system (the p99 commit that
    waited on an fsync is the one a user notices).

Honest scope: single process, single connection, local Postgres. This measures
the store's own overhead, not a distributed deployment. A Temporal-wrapped
baseline is the intended next comparison (see README roadmap).

Run:
    python bench/benchmark.py --iterations 2000                # in-memory only
    python bench/benchmark.py --conninfo "host=... dbname=..." # + Postgres
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

from hapax.memory import InMemoryTaskStore
from hapax.task import TaskState


@dataclass
class Stat:
    op: str
    backend: str
    n: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    throughput_ops_s: float


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolation percentile. `sorted_vals` must be pre-sorted."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _summarize(op: str, backend: str, samples_s: list[float]) -> Stat:
    ms = sorted(s * 1000.0 for s in samples_s)
    total_s = sum(samples_s)
    n = len(ms)
    return Stat(
        op=op,
        backend=backend,
        n=n,
        mean_ms=(sum(ms) / n) if n else 0.0,
        p50_ms=_percentile(ms, 50),
        p95_ms=_percentile(ms, 95),
        p99_ms=_percentile(ms, 99),
        max_ms=ms[-1] if ms else 0.0,
        throughput_ops_s=(n / total_s) if total_s > 0 else 0.0,
    )


def _new_store(backend: str, conninfo: str | None):
    if backend == "memory":
        return InMemoryTaskStore()
    from hapax.postgres import PostgresTaskStore

    s = PostgresTaskStore(conninfo)  # type: ignore[arg-type]
    with s._conn.cursor() as cur:
        cur.execute("TRUNCATE tasks")
    s._conn.commit()
    return s


def bench_backend(
    backend: str, *, conninfo: str | None, iterations: int, warmup: int
) -> list[Stat]:
    store = _new_store(backend, conninfo)
    stats: list[Stat] = []

    # --- create ---------------------------------------------------------------
    for i in range(warmup):
        store.create_task({"op": "warmup", "i": i})
    samples: list[float] = []
    for i in range(iterations):
        t0 = time.perf_counter()
        store.create_task({"op": "bench", "i": i})
        samples.append(time.perf_counter() - t0)
    stats.append(_summarize("create", backend, samples))

    # --- get (one hot task, read repeatedly) ----------------------------------
    hot = store.create_task({"op": "hot"})
    for _ in range(warmup):
        store.get_task(hot.id)
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        store.get_task(hot.id)
        samples.append(time.perf_counter() - t0)
    stats.append(_summarize("get", backend, samples))

    # --- update (working -> completed), isolated from create ------------------
    # Pre-create the tasks untimed so only the transition is measured.
    to_update = [store.create_task({"op": "upd", "i": i}).id for i in range(iterations)]
    warm = [store.create_task({"op": "updw", "i": i}).id for i in range(warmup)]
    for tid in warm:
        store.update_task(tid, TaskState.COMPLETED, result={"ok": True})
    samples = []
    for tid in to_update:
        t0 = time.perf_counter()
        store.update_task(tid, TaskState.COMPLETED, result={"ok": True})
        samples.append(time.perf_counter() - t0)
    stats.append(_summarize("update", backend, samples))

    if hasattr(store, "close"):
        store.close()
    return stats


def render_markdown(all_stats: list[Stat]) -> str:
    lines = [
        "| backend | op | n | mean (ms) | p50 | p95 | p99 | max | throughput (ops/s) |",
        "|---------|----|---|-----------|-----|-----|-----|-----|--------------------|",
    ]
    for s in all_stats:
        lines.append(
            f"| {s.backend} | {s.op} | {s.n} | {s.mean_ms:.3f} | {s.p50_ms:.3f} | "
            f"{s.p95_ms:.3f} | {s.p99_ms:.3f} | {s.max_ms:.3f} | {s.throughput_ops_s:,.0f} |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Task store benchmark.")
    ap.add_argument("--conninfo", default=None, help="Postgres conn string; omit to skip")
    ap.add_argument("--iterations", type=int, default=2000)
    ap.add_argument("--warmup", type=int, default=200)
    args = ap.parse_args()

    backends = ["memory"] + (["postgres"] if args.conninfo else [])
    all_stats: list[Stat] = []
    for backend in backends:
        print(f"benchmarking {backend} ...", flush=True)
        all_stats.extend(
            bench_backend(
                backend,
                conninfo=args.conninfo,
                iterations=args.iterations,
                warmup=args.warmup,
            )
        )

    table = render_markdown(all_stats)
    print("\n" + table)


if __name__ == "__main__":
    main()
