"""Chaos test: kill the worker at uniformly random points and check the invariant.

The crash-recovery tests kill the worker at three *chosen* points — before the
effect, before the commit, after the commit — because those are the boundaries
the design reasons about. That is exactly the weakness of a hand-picked test:
it proves the guarantee holds where the author thought to look.

This harness doesn't choose. Each trial starts a real worker doing a job of
known duration and hard-kills it from the outside at a uniformly random moment
in that window, so kills land wherever they land — including inside the commit
itself, which is the interval no hand-written test can target reliably. Then
recovery runs and the invariant is checked against the database:

    the customer was charged exactly once — not zero times, not twice

Any trial that ends any other way is a violation and is reported with the
timing that produced it, so it can be replayed.

    python bench/chaos.py --conninfo "host=… dbname=…" --trials 200

The kill is delivered by the parent with SIGKILL, so the worker gets no
opportunity to clean up, flush, or roll anything back. Postgres finds out its
client is gone the same way it would if the machine had lost power.
"""

from __future__ import annotations

import argparse
import random
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hapax.dispatcher import Dispatcher
from hapax.postgres import PostgresTaskStore
from hapax.worker import ensure_ledger, process_charge

AMOUNT = 50


REPO_ROOT = Path(__file__).resolve().parent.parent
NAIVE_WORKER = REPO_ROOT / "scripts" / "naive_worker.py"


def _spawn(conninfo: str, task_id: str, work_seconds: float, strategy: str = "hapax",
           lease_seconds: float | None = None) -> subprocess.Popen:
    if strategy == "hapax":
        cmd = [sys.executable, "-m", "hapax.worker"]
        if lease_seconds is not None:
            cmd += ["--lease-seconds", str(lease_seconds)]
    else:
        cmd = [sys.executable, str(NAIVE_WORKER)]
    return subprocess.Popen(
        cmd + [
            "--conninfo", conninfo,
            "--task-id", task_id,
            "--work-seconds", str(work_seconds),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _ledger_rows(conn: psycopg.Connection, task_id: str, strategy: str = "hapax") -> tuple[int, int]:
    table = "ledger" if strategy == "hapax" else "naive_ledger"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*), coalesce(sum(amount), 0) FROM {table} WHERE task_id = %s", [task_id]
        )
        n, total = cur.fetchone()
    conn.commit()
    return int(n), int(total)


def measure_lifetime(store: PostgresTaskStore, conninfo: str, work_seconds: float,
                     n: int = 5, strategy: str = "hapax") -> float:
    """How long does an uninterrupted worker actually live, start to exit?

    Kills have to be spread across that whole span to be meaningful. Spreading
    them across the *job* duration alone would land every one of them in Python
    interpreter startup, before the worker has touched the database — which
    proves nothing except that a process that never started cannot double-charge.
    """
    times = []
    for j in range(n):
        if strategy == "hapax":
            tid = store.create_task({"op": "charge"}, idempotency_key=f"chaos-calibrate-{j}").id
        else:
            tid = f"chaos-calibrate-{j}"
        t0 = time.perf_counter()
        _spawn(conninfo, tid, work_seconds, strategy).wait(timeout=60)
        times.append(time.perf_counter() - t0)
    times.sort()
    return times[len(times) // 2]


def run_trial(
    store: PostgresTaskStore,
    conn: psycopg.Connection,
    conninfo: str,
    i: int,
    rng: random.Random,
    work_seconds: float,
    window: float,
    strategy: str = "hapax",
    recovery: str = "manual",
    lease_seconds: float = 0.25,
) -> dict:
    if strategy == "hapax":
        task_id = store.create_task({"op": "charge"}, idempotency_key=f"chaos-{i}").id
    else:
        task_id = f"chaos-naive-{i}"

    # Uniform across the worker's whole measured lifetime, slightly overshot so
    # some kills land after it has already finished — also a case recovery has
    # to survive.
    kill_at = rng.uniform(0.0, window)

    proc = _spawn(conninfo, task_id, work_seconds, strategy,
                  lease_seconds=lease_seconds if recovery == "dispatcher" else None)
    time.sleep(kill_at)
    killed = proc.poll() is None
    if killed:
        proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)

    # State the crash left behind, before anything tries to clean it up.
    rows_after_crash, _ = _ledger_rows(conn, task_id, strategy)
    state_after_crash = store.get_task(task_id).state.value if strategy == "hapax" else "n/a"

    recovered_in_ms = None
    if recovery == "dispatcher":
        # Nobody retries anything. Wait for the dead worker's lease to lapse and
        # let a dispatcher sweep find the task, timing how long that takes from
        # the moment of death.
        t0 = time.perf_counter()
        dispatcher = Dispatcher(
            store=store,
            handler=lambda t: process_charge(conninfo, t.id, lease_seconds=lease_seconds),
            lease_seconds=30,
        )
        deadline = t0 + 30
        while time.perf_counter() < deadline:
            if dispatcher.sweep(limit=1) > 0:
                break
            if store.get_task(task_id).is_terminal:
                break
            time.sleep(0.01)
        recovered_in_ms = round((time.perf_counter() - t0) * 1000, 1)
        rec = subprocess.CompletedProcess(args=[], returncode=0, stdout="dispatcher", stderr="")
    else:
        # Recovery: the same thing a queue or supervisor would do.
        rec_cmd = (
            [sys.executable, "-m", "hapax.worker"] if strategy == "hapax"
            else [sys.executable, str(NAIVE_WORKER)]
        )
        rec = subprocess.run(
            rec_cmd + ["--conninfo", conninfo, "--task-id", task_id],
            capture_output=True, text=True, timeout=60,
        )

    rows, total = _ledger_rows(conn, task_id, strategy)
    state = store.get_task(task_id).state.value if strategy == "hapax" else "n/a"

    ok = rows == 1 and total == AMOUNT and (state == "completed" or strategy == "naive")
    return {
        "i": i,
        "ok": ok,
        "kill_at_ms": round(kill_at * 1000, 1),
        "killed": killed,
        "rows_after_crash": rows_after_crash,
        "state_after_crash": state_after_crash,
        "rows": rows,
        "total": total,
        "state": state,
        "recovery_said": rec.stdout.strip(),
        "recovered_in_ms": recovered_in_ms,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conninfo", required=True)
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--work-seconds", type=float, default=0.05,
                    help="length of the job the kill has to land inside")
    ap.add_argument("--seed", type=int, default=1234, help="fixed so runs are reproducible")
    ap.add_argument("--strategy", choices=["hapax", "naive"], default="hapax",
                    help="'naive' runs the identical experiment against the control group")
    ap.add_argument("--recovery", choices=["manual", "dispatcher"], default="manual",
                    help="'dispatcher' lets a lapsed lease trigger recovery instead of "
                         "re-running the worker by hand")
    ap.add_argument("--lease-seconds", type=float, default=0.25,
                    help="lease the worker takes, in dispatcher mode")
    args = ap.parse_args()

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from naive_worker import ensure_naive_ledger

    store = PostgresTaskStore(args.conninfo)
    ensure_ledger(store)
    conn = psycopg.connect(args.conninfo)
    ensure_naive_ledger(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE tasks, ledger, naive_ledger")
    conn.commit()

    rng = random.Random(args.seed)
    results = []
    buckets: Counter[str] = Counter()

    lifetime = measure_lifetime(store, args.conninfo, args.work_seconds, strategy=args.strategy)
    window = lifetime * 1.15
    with conn.cursor() as cur:
        cur.execute("TRUNCATE tasks, ledger, naive_ledger")
    conn.commit()

    started = time.perf_counter()
    print(f"chaos [{args.strategy}, recovery={args.recovery}]: "
          f"{args.trials} trials, seed={args.seed}")
    print(f"worker lifetime measured at {lifetime * 1000:.0f} ms; "
          f"SIGKILL at a uniformly random point in [0, {window * 1000:.0f} ms)\n")

    for i in range(args.trials):
        r = run_trial(store, conn, args.conninfo, i, rng, args.work_seconds, window,
                      args.strategy, args.recovery, args.lease_seconds)
        results.append(r)

        # Where did the kill actually land? Read it off the state it left behind.
        if not r["killed"]:
            buckets["after the worker had already finished"] += 1
        elif r["rows_after_crash"] == 1:
            buckets["after the charge committed"] += 1
        else:
            buckets["before the charge committed"] += 1

        if not r["ok"]:
            print(f"  VIOLATION trial {i}: {r}")
        if (i + 1) % 25 == 0:
            bad = sum(1 for x in results if not x["ok"])
            print(f"  {i + 1}/{args.trials} trials, {bad} violation(s) so far")

    elapsed = time.perf_counter() - started
    conn.close()
    store.close()

    violations = [r for r in results if not r["ok"]]
    charged_once = sum(1 for r in results if r["rows"] == 1 and r["total"] == AMOUNT)

    print(f"\n{'=' * 62}")
    print(f"trials                     {len(results)}")
    print(f"charged exactly once       {charged_once}/{len(results)}")
    print(f"double charges             {sum(1 for r in results if r['rows'] > 1)}")
    print(f"lost charges               {sum(1 for r in results if r['rows'] == 0)}")
    print(f"violations                 {len(violations)}")
    print(f"wall clock                 {elapsed:.1f}s")
    times = sorted(r["recovered_in_ms"] for r in results if r["recovered_in_ms"] is not None)
    if times:
        print(f"\ntime from crash to automatic recovery (lease {args.lease_seconds}s):")
        print(f"  p50  {times[len(times) // 2]:.0f} ms")
        print(f"  p95  {times[int(len(times) * 0.95)]:.0f} ms")
        print(f"  max  {times[-1]:.0f} ms")

    print("\nwhere the kills landed:")
    for label, n in buckets.most_common():
        print(f"  {n:>4}  {label}")

    if violations:
        sys.exit(1)


if __name__ == "__main__":
    main()
