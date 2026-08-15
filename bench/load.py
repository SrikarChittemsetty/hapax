"""Load test: how many concurrent workers can this store actually feed?

Everything else in bench/ measures correctness under adversity. This measures
capacity, which is the other half of the question and the half that was missing:
the latency benchmark runs one process on one connection, so it reports the
store's floor, not what happens when twenty workers fight over the same table.

Each trial pre-seeds N tasks, then starts W worker *processes* — real processes,
not threads, so the GIL is not silently serialising the thing being measured.
Every worker loops: claim a task, do the side effect, commit, repeat, until the
queue drains. What comes out is throughput and latency percentiles per worker
count, which is what you need to find the point where adding workers stops
helping.

    python bench/load.py --conninfo "host=… dbname=…" --tasks 2000 --workers 1,2,4,8,16

Each worker times its own operations and reports them back through a JSON file,
so the parent process never sits in the measurement path.

Correctness is re-checked at every concurrency level, because a throughput number
from a run that double-charged somebody is worse than no number at all.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hapax.postgres import PostgresTaskStore
from hapax.worker import ensure_ledger

AMOUNT = 50


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(len(ordered) * p), len(ordered) - 1)
    return ordered[idx]


# --- the worker process -------------------------------------------------------


def run_worker(conninfo: str, out_path: str, lease_seconds: float, gate: str | None) -> None:
    """Drain the queue, timing each claim and each completion separately.

    Splitting the two is the whole diagnostic: if throughput stops scaling, the
    answer is either that workers are queueing to *get* work (contention on the
    claim) or queueing to *finish* it (contention on commit), and the only way
    to tell is to measure them apart.
    """
    store = PostgresTaskStore(conninfo)

    # Barrier. Interpreter startup and connecting to Postgres cost ~150 ms per
    # worker, which at sixteen workers is more than the whole drain takes — left
    # inside the timed window it would show up as a throughput collapse that is
    # really just process startup. So: signal ready, then wait to be released,
    # and let the parent start its clock once everyone is actually working.
    if gate:
        ready = Path(gate + f".ready.{Path(out_path).stem}")
        ready.write_text("1")
        go = Path(gate + ".go")
        while not go.exists():
            time.sleep(0.002)

    claim_ms: list[float] = []
    work_ms: list[float] = []
    done = 0

    def charge(task_id: str):
        def effect(cur: psycopg.Cursor) -> None:
            cur.execute(
                "INSERT INTO ledger (task_id, amount) VALUES (%s, %s)"
                " ON CONFLICT (task_id) DO NOTHING",
                [task_id, AMOUNT],
            )
        return effect

    started = time.perf_counter()
    while True:
        t0 = time.perf_counter()
        task = store.claim_task(lease_seconds=lease_seconds)
        t1 = time.perf_counter()
        claim_ms.append((t1 - t0) * 1000)
        if task is None:
            break

        store.complete_with_effect(task.id, charge(task.id), result={"charged": AMOUNT})
        work_ms.append((time.perf_counter() - t1) * 1000)
        done += 1

    elapsed = time.perf_counter() - started
    store.close()

    Path(out_path).write_text(json.dumps({
        "done": done,
        "elapsed_s": elapsed,
        "claim_ms": claim_ms,
        "work_ms": work_ms,
    }))


# --- one concurrency level ----------------------------------------------------


def run_level(conninfo: str, workers: int, tasks: int, lease_seconds: float) -> dict:
    store = PostgresTaskStore(conninfo)
    ensure_ledger(store)
    with store._conn.cursor() as cur:
        cur.execute("TRUNCATE tasks, ledger")
    store._conn.commit()

    for i in range(tasks):
        store.create_task({"op": "charge"}, idempotency_key=f"load-{workers}-{i}")

    with tempfile.TemporaryDirectory() as tmp:
        paths = [str(Path(tmp) / f"w{w}.json") for w in range(workers)]
        gate = str(Path(tmp) / "gate")

        procs = [
            subprocess.Popen(
                [sys.executable, __file__, "--worker-mode",
                 "--conninfo", conninfo, "--out", p,
                 "--lease-seconds", str(lease_seconds), "--gate", gate],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for p in paths
        ]

        # Wait until every worker has booted and connected, then release them all
        # at once. The clock starts here, so it measures draining the queue and
        # not starting Python sixteen times.
        deadline = time.perf_counter() + 120
        expected = {Path(gate + f".ready.{Path(p).stem}") for p in paths}
        while time.perf_counter() < deadline:
            if all(r.exists() for r in expected):
                break
            dead = [pr for pr in procs if pr.poll() not in (None, 0)]
            if dead:
                raise RuntimeError(f"worker died before starting: {dead[0].stderr.read()[:400]}")
            time.sleep(0.005)
        else:
            raise RuntimeError("workers never became ready")

        t0 = time.perf_counter()
        Path(gate + ".go").write_text("1")
        for proc in procs:
            proc.wait(timeout=600)
        wall = time.perf_counter() - t0

        failures = [p for p in procs if p.returncode != 0]
        if failures:
            raise RuntimeError(f"worker failed: {failures[0].stderr.read()[:400]}")

        reports = [json.loads(Path(p).read_text()) for p in paths]

    claim_ms = [v for r in reports for v in r["claim_ms"]]
    work_ms = [v for r in reports for v in r["work_ms"]]
    completed = sum(r["done"] for r in reports)

    # Correctness, re-checked at every level: exactly one ledger row per task,
    # and the money adds up.
    with store._conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n, coalesce(sum(amount), 0) AS s FROM ledger")
        row = cur.fetchone()
        ledger_rows, ledger_total = int(row["n"]), int(row["s"])
        cur.execute("SELECT count(*) AS n FROM tasks WHERE state <> 'completed'")
        unfinished = int(cur.fetchone()["n"])
    store._conn.commit()
    store.close()

    return {
        "workers": workers,
        "tasks": tasks,
        "completed": completed,
        "wall_s": round(wall, 3),
        "throughput": round(completed / wall, 1) if wall else 0.0,
        "claim_p50": round(percentile(claim_ms, 0.50), 3),
        "claim_p99": round(percentile(claim_ms, 0.99), 3),
        "work_p50": round(percentile(work_ms, 0.50), 3),
        "work_p99": round(percentile(work_ms, 0.99), 3),
        "total_p50": round(percentile(claim_ms, 0.50) + percentile(work_ms, 0.50), 3),
        "total_p99": round(percentile(claim_ms, 0.99) + percentile(work_ms, 0.99), 3),
        "ledger_rows": ledger_rows,
        "ledger_total": ledger_total,
        "unfinished": unfinished,
        "exactly_once": ledger_rows == tasks and ledger_total == tasks * AMOUNT and unfinished == 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conninfo", required=True)
    ap.add_argument("--tasks", type=int, default=2000)
    ap.add_argument("--workers", default="1,2,4,8,16,32")
    ap.add_argument("--lease-seconds", type=float, default=60)
    ap.add_argument("--repeat", type=int, default=1,
                    help="runs per worker count; the median is reported, because "
                         "a single run of this varies by 30%% or more")
    ap.add_argument("--out", default=None, help="worker mode: where to write the report")
    ap.add_argument("--worker-mode", action="store_true")
    ap.add_argument("--gate", default=None, help="worker mode: readiness barrier path")
    args = ap.parse_args()

    if args.worker_mode:
        run_worker(args.conninfo, args.out, args.lease_seconds, args.gate)
        return

    levels = [int(w) for w in args.workers.split(",")]
    print(f"load: {args.tasks} tasks per level, worker counts {levels}\n")
    print(f"{'workers':>7} {'tasks/s':>9} {'claim p50':>10} {'claim p99':>10} "
          f"{'work p50':>9} {'work p99':>9} {'wall':>7}  correct")
    print("-" * 78)

    results = []
    for w in levels:
        # Repeat and take the median run. One run of this on a laptop varies by
        # a third depending on what else the machine feels like doing, and a
        # saturation curve drawn through single noisy samples invents cliffs
        # that are not there.
        runs = [run_level(args.conninfo, w, args.tasks, args.lease_seconds)
                for _ in range(args.repeat)]
        runs.sort(key=lambda r: r["throughput"])
        r = runs[len(runs) // 2]
        r["runs"] = args.repeat
        r["throughput_min"] = min(x["throughput"] for x in runs)
        r["throughput_max"] = max(x["throughput"] for x in runs)
        r["exactly_once"] = all(x["exactly_once"] for x in runs)
        results.append(r)
        print(f"{r['workers']:>7} {r['throughput']:>9.1f} {r['claim_p50']:>9.3f}m "
              f"{r['claim_p99']:>9.3f}m {r['work_p50']:>8.3f}m {r['work_p99']:>8.3f}m "
              f"{r['wall_s']:>6.2f}s  {'yes' if r['exactly_once'] else 'NO — ' + str(r['ledger_rows'])}")

    best = max(results, key=lambda r: r["throughput"])
    print(f"\npeak throughput {best['throughput']:.1f} tasks/s at {best['workers']} workers")

    # Where did the extra time go once it stopped scaling?
    print("\nscaling relative to one worker:")
    base = results[0]
    for r in results:
        speedup = r["throughput"] / base["throughput"] if base["throughput"] else 0
        ideal = r["workers"] / base["workers"]
        print(f"  {r['workers']:>3} workers: {speedup:5.2f}x  "
              f"(perfect scaling would be {ideal:.0f}x, "
              f"efficiency {100 * speedup / ideal:.0f}%)")

    out = Path(__file__).parent / "load_results.json"
    out.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nwrote {out}")

    if not all(r["exactly_once"] for r in results):
        print("\nCORRECTNESS FAILURE — a throughput number from a run that "
              "double-charged is worthless")
        sys.exit(1)


if __name__ == "__main__":
    main()
