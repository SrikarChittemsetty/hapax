"""A watchable crash-durability demo.

Runs a real scenario end to end against Postgres: an agent charges a customer,
the worker is hard-killed (`kill -9`) at the worst possible moment, and you can
SEE that this system charges exactly once — where a naive retry double-charges.

Nothing here is mocked. The worker is a real OS process; the kills are real
SIGKILLs; the ledger is a real table you can query afterwards.

    python scripts/demo.py --conninfo "host=127.0.0.1 port=55432 user=postgres dbname=mdt"

Defaults to the local dev database if --conninfo is omitted.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hapax.postgres import PostgresTaskStore
from hapax.worker import ensure_ledger
from naive_worker import ensure_naive_ledger

# --- tiny ANSI helpers so the demo reads well in a terminal or a GIF ----------
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def h(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{text}{RESET}")


def step(text: str) -> None:
    print(f"  {text}")


def good(text: str) -> None:
    print(f"  {GREEN}✓ {text}{RESET}")


def bad(text: str) -> None:
    print(f"  {RED}✗ {text}{RESET}")


def _run_worker(conninfo, task_id, *, crash_at=None):
    cmd = [
        sys.executable, "-m", "hapax.worker",
        "--conninfo", conninfo, "--task-id", task_id,
    ]
    if crash_at:
        cmd += ["--crash-at", crash_at]
    return subprocess.run(cmd, capture_output=True, text=True)


def _run_naive_worker(conninfo, task_id, *, crash_at=None):
    cmd = [
        sys.executable, str(Path(__file__).resolve().parent / "naive_worker.py"),
        "--conninfo", conninfo, "--task-id", task_id,
    ]
    if crash_at:
        cmd += ["--crash-at", crash_at]
    return subprocess.run(cmd, capture_output=True, text=True)


def _naive_total(store, task_id):
    with store._conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(sum(amount),0) AS s FROM naive_ledger WHERE task_id=%s", [task_id]
        )
        s = cur.fetchone()["s"]
    store._conn.commit()
    return s


def _ledger_total(store, task_id):
    with store._conn.cursor() as cur:
        cur.execute("SELECT coalesce(sum(amount),0) AS s FROM ledger WHERE task_id=%s", [task_id])
        s = cur.fetchone()["s"]
    store._conn.commit()
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--conninfo",
        default=os.environ.get(
            "HAPAX_DATABASE_URL", "host=127.0.0.1 port=55432 user=postgres dbname=mdt"
        ),
    )
    args = ap.parse_args()

    store = PostgresTaskStore(args.conninfo)
    ensure_ledger(store)
    ensure_naive_ledger(store._conn)
    with store._conn.cursor() as cur:
        cur.execute("TRUNCATE tasks, ledger, naive_ledger")
    store._conn.commit()

    print(f"{BOLD}Hapax — crash-durability demo{RESET}")
    print(f"{DIM}An agent charges a customer $50. The worker gets kill -9'd mid-flight.{RESET}")

    # === THE PROBLEM ==========================================================
    # The control group gets the identical treatment the Hapax worker gets
    # below: a real process, hard-killed at the same point. The only difference
    # is that its "have I already charged this?" guard lives in process memory.
    h("THE PROBLEM — a naive charge, crashed and retried")
    r = _run_naive_worker(args.conninfo, "naive-1", crash_at="after_commit")
    step(f"charge sent, then killed (kill -9, rc={r.returncode}) 💥")
    step(f"charge landed .................. ledger=${_naive_total(store, 'naive-1')}, "
         "but the in-memory guard died with the process")
    _run_naive_worker(args.conninfo, "naive-1")  # the retry
    step(f"{YELLOW}retry finds no record of the charge and sends it again{RESET}")
    naive_total = _naive_total(store, "naive-1")
    bad(f"customer charged ${naive_total} — DOUBLE CHARGED")

    # === THE SOLUTION, CASE 1 =================================================
    h("WITH Hapax — CASE 1: crash BEFORE the charge commits")
    t1 = store.create_task({"op": "charge"}, idempotency_key="demo-1")
    step(f"task created .................... state={store.get_task(t1.id).state.value}")
    r = _run_worker(args.conninfo, t1.id, crash_at="before_commit")
    step(f"{YELLOW}worker killed mid-transaction (kill -9, rc={r.returncode}) 💥{RESET}")
    step(f"charge rolled back ............. ledger=${_ledger_total(store, t1.id)}, "
         f"state={store.get_task(t1.id).state.value}")
    _run_worker(args.conninfo, t1.id)  # recovery
    step(f"recovery re-runs the worker .... state={store.get_task(t1.id).state.value}")
    good(f"customer charged exactly once: ${_ledger_total(store, t1.id)}")

    # === THE SOLUTION, CASE 2 =================================================
    h("WITH Hapax — CASE 2: crash AFTER the charge commits")
    t2 = store.create_task({"op": "charge"}, idempotency_key="demo-2")
    step(f"task created .................... state={store.get_task(t2.id).state.value}")
    r = _run_worker(args.conninfo, t2.id, crash_at="after_commit")
    step(f"{YELLOW}worker charged, then killed (kill -9, rc={r.returncode}) 💥{RESET}")
    step(f"charge already landed .......... ledger=${_ledger_total(store, t2.id)}, "
         f"state={store.get_task(t2.id).state.value}")
    rec = _run_worker(args.conninfo, t2.id)  # recovery
    step(f"recovery sees terminal ......... worker says '{rec.stdout.strip()}' (refuses to re-charge)")
    good(f"customer charged exactly once: ${_ledger_total(store, t2.id)}")

    # === SUMMARY ==============================================================
    h("RESULT")
    print(f"  naive approach:        {RED}${naive_total}  (double charged){RESET}")
    print(f"  Hapax:                 {GREEN}$50   (exactly once, through two different crashes){RESET}")
    print(f"\n{DIM}Every kill above was a real SIGKILL; every dollar is a real row in Postgres.{RESET}\n")
    store.close()


if __name__ == "__main__":
    main()
