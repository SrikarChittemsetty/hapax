"""Run the crash-durability scenarios for real and record exactly what happened.

This is the demo, minus the pretty printing, plus evidence. Every step it writes
out carries the thing that actually produced it — the command line that was run,
the OS process id, the exit status the kernel reported, or the SQL query and the
row it returned. The web replay at docs/index.html renders this file and nothing
else, so the animation a visitor watches is a recording of a real run rather than
a hand-written story about one.

    python scripts/record_trace.py --conninfo "host=127.0.0.1 user=postgres dbname=mdt"

Writes docs/trace.json. Re-run it any time; the demo page updates with it.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from hapax.postgres import PostgresTaskStore
from hapax.worker import ensure_ledger
from naive_worker import ensure_naive_ledger  # noqa: E402  (scripts/ is on sys.path)

REPO_ROOT = Path(__file__).resolve().parent.parent
AMOUNT = 50


class Recorder:
    """Collects timed, evidence-carrying steps for one scenario."""

    def __init__(self, key: str, title: str, subtitle: str, approach: str) -> None:
        self.key = key
        self.title = title
        self.subtitle = subtitle
        self.approach = approach  # "naive" | "hapax"
        self.steps: list[dict] = []
        self._t0 = time.perf_counter()

    def add(
        self,
        kind: str,
        title: str,
        detail: str = "",
        *,
        proof: dict | None = None,
        ledger: int | None = None,
        task_state: str | None = None,
    ) -> None:
        self.steps.append(
            {
                "n": len(self.steps) + 1,
                "kind": kind,  # start | work | crash | observe | recover | verdict
                "title": title,
                "detail": detail,
                "proof": proof,
                "ledger": ledger,
                "task_state": task_state,
                "at_ms": round((time.perf_counter() - self._t0) * 1000, 1),
            }
        )

    def finish(self, charged: int, verdict: str, ok: bool) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "subtitle": self.subtitle,
            "approach": self.approach,
            "steps": self.steps,
            "result": {"charged": charged, "expected": AMOUNT, "verdict": verdict, "ok": ok},
        }


# --- helpers that both run the thing and capture the evidence ----------------


def run_worker(
    module_or_script: str, conninfo: str, task_id: str, *, crash_at: str | None = None
) -> tuple[subprocess.CompletedProcess, dict]:
    """Spawn a real worker process. Returns (result, proof)."""
    if module_or_script == "hapax":
        cmd = [sys.executable, "-m", "hapax.worker"]
        shown = "python -m hapax.worker"
    else:
        cmd = [sys.executable, str(REPO_ROOT / "scripts" / "naive_worker.py")]
        shown = "python scripts/naive_worker.py"

    cmd += ["--conninfo", conninfo, "--task-id", task_id]
    shown += f" --task-id {task_id}"
    if crash_at:
        cmd += ["--crash-at", crash_at]
        shown += f" --crash-at {crash_at}"

    proc = subprocess.run(cmd, capture_output=True, text=True)

    # A process killed by signal N reports returncode -N. SIGKILL is 9.
    if proc.returncode < 0:
        outcome = f"killed by signal {-proc.returncode} (SIGKILL)"
    else:
        outcome = f"exit {proc.returncode}" + (
            f", printed {proc.stdout.strip()!r}" if proc.stdout.strip() else ""
        )

    proof = {"kind": "shell", "cmd": f"$ {shown}", "out": outcome}
    return proc, proof


def sql_scalar(conn: psycopg.Connection, sql: str, params: list) -> tuple[int, dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        value = list(row.values())[0] if isinstance(row, dict) else row[0]
    conn.commit()
    shown = " ".join(sql.split())
    for p in params:
        shown = shown.replace("%s", f"'{p}'", 1)
    return int(value), {"kind": "sql", "cmd": shown, "out": f"{value}"}


def ledger_total(conn: psycopg.Connection, task_id: str) -> tuple[int, dict]:
    return sql_scalar(
        conn,
        "SELECT coalesce(sum(amount), 0) FROM ledger WHERE task_id = %s",
        [task_id],
    )


def naive_ledger_total(conn: psycopg.Connection, task_id: str) -> tuple[int, dict]:
    return sql_scalar(
        conn,
        "SELECT coalesce(sum(amount), 0) FROM naive_ledger WHERE task_id = %s",
        [task_id],
    )


def task_state(store: PostgresTaskStore, task_id: str) -> tuple[str, dict]:
    state = store.get_task(task_id).state.value
    return state, {
        "kind": "sql",
        "cmd": f"SELECT state FROM tasks WHERE id = '{task_id}'",
        "out": state,
    }


# --- the three scenarios ------------------------------------------------------


def scenario_naive(conninfo: str, conn: psycopg.Connection) -> dict:
    r = Recorder(
        key="naive",
        title="The way most people write it",
        subtitle="Dedup state lives in process memory",
        approach="naive",
    )
    task_id = "naive-1"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM naive_ledger WHERE task_id = %s", [task_id])
    conn.commit()

    r.add("start", "A customer needs to be charged $50.",
          "The worker keeps a set of task ids it has already charged, so a retry "
          "will not charge twice.", ledger=0)

    proc, proof = run_worker("naive", conninfo, task_id, crash_at="after_commit")
    total, _ = naive_ledger_total(conn, task_id)
    r.add("crash", "The worker is hard-killed the instant after the charge commits.",
          "kill -9 — the process gets no chance to clean up, and its in-memory set "
          "of already-charged tasks dies with it.",
          proof=proof, ledger=total)

    total, tproof = naive_ledger_total(conn, task_id)
    r.add("observe", f"The charge did land: ${total}.",
          "Postgres has the row. The worker's memory of having made it does not exist "
          "anywhere anymore.", proof=tproof, ledger=total)

    proc, proof = run_worker("naive", conninfo, task_id)
    r.add("recover", "A retry starts, as any queue or supervisor would do.",
          "New process, fresh empty set. It checks whether it already charged this "
          "task, finds nothing, and charges again.", proof=proof)

    total, tproof = naive_ledger_total(conn, task_id)
    r.add("verdict", f"The customer was charged ${total}.",
          "Two rows in the ledger for one job. This is a real double charge, not a "
          "hypothetical one.", proof=tproof, ledger=total)

    return r.finish(total, "double charged", ok=(total == AMOUNT))


def scenario_before_commit(conninfo: str, conn: psycopg.Connection, store) -> dict:
    r = Recorder(
        key="hapax_before_commit",
        title="Hapax, killed before the charge commits",
        subtitle="The worst moment: mid-transaction",
        approach="hapax",
    )
    task = store.create_task({"op": "charge"}, idempotency_key="trace-before-commit")
    state, sproof = task_state(store, task.id)
    r.add("start", "The same $50 charge, now as a Hapax task.",
          "The task's state lives in Postgres, not in the worker.",
          proof=sproof, ledger=0, task_state=state)

    proc, proof = run_worker("hapax", conninfo, task.id, crash_at="before_commit")
    r.add("crash", "The worker is hard-killed mid-transaction.",
          "The charge has been written but not committed. The kill is a real SIGKILL, "
          "so the database connection simply drops.", proof=proof)

    total, tproof = ledger_total(conn, task.id)
    state, sproof = task_state(store, task.id)
    r.add("observe", f"Postgres rolled the whole transaction back: ledger ${total}.",
          "The charge and the 'this task is done' flag were written in one transaction, "
          f"so neither survived. The task is honestly still {state}.",
          proof=tproof, ledger=total, task_state=state)

    proc, proof = run_worker("hapax", conninfo, task.id)
    state, _ = task_state(store, task.id)
    r.add("recover", "A retry picks the task up and runs it cleanly.",
          "Nothing was half-done, so there is nothing to reconcile.",
          proof=proof, task_state=state)

    total, tproof = ledger_total(conn, task.id)
    state, sproof = task_state(store, task.id)
    r.add("verdict", f"The customer was charged ${total}. Exactly once.",
          "One row. The crash cost a retry, not a duplicate charge.",
          proof=tproof, ledger=total, task_state=state)

    return r.finish(total, "charged exactly once", ok=(total == AMOUNT))


def scenario_after_commit(conninfo: str, conn: psycopg.Connection, store) -> dict:
    r = Recorder(
        key="hapax_after_commit",
        title="Hapax, killed after the charge commits",
        subtitle="The moment that breaks the naive version",
        approach="hapax",
    )
    task = store.create_task({"op": "charge"}, idempotency_key="trace-after-commit")
    state, sproof = task_state(store, task.id)
    r.add("start", "The same $50 charge again.",
          "This time the kill lands after the money has already moved — the case that "
          "double-charged the naive worker.",
          proof=sproof, ledger=0, task_state=state)

    proc, proof = run_worker("hapax", conninfo, task.id, crash_at="after_commit")
    r.add("crash", "The worker charges, commits, and is hard-killed immediately after.",
          "Same kill -9, same instant as the naive run.", proof=proof)

    total, tproof = ledger_total(conn, task.id)
    state, sproof = task_state(store, task.id)
    r.add("observe", f"The charge landed (${total}) and the task is {state}.",
          "Both were committed together, so the record of the work is as durable as "
          "the work itself.", proof=tproof, ledger=total, task_state=state)

    proc, proof = run_worker("hapax", conninfo, task.id)
    r.add("recover", "The retry starts and immediately declines to do anything.",
          "It reads the task from Postgres, sees a terminal state, and stops. "
          "'noop-terminal' is the worker refusing to charge a second time.",
          proof=proof)

    total, tproof = ledger_total(conn, task.id)
    state, sproof = task_state(store, task.id)
    r.add("verdict", f"The customer was charged ${total}. Exactly once.",
          "Same crash that cost the naive worker $50 of someone else's money.",
          proof=tproof, ledger=total, task_state=state)

    return r.finish(total, "charged exactly once", ok=(total == AMOUNT))


# --- environment capture ------------------------------------------------------


def capture_env(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        row = cur.fetchone()
        pg = list(row.values())[0] if isinstance(row, dict) else row[0]
    conn.commit()

    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
            ).stdout.strip()
        except Exception:
            return "unknown"

    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "postgres": pg.split(" on ")[0],
        "python": f"{platform.python_version()} ({platform.machine()})",
        "os": f"{platform.system()} {platform.release()}",
        "commit": git("rev-parse", "--short", "HEAD"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--conninfo",
        default=os.environ.get(
            "HAPAX_DATABASE_URL", "host=127.0.0.1 port=5432 user=postgres dbname=mdt"
        ),
    )
    ap.add_argument("--out", default=str(REPO_ROOT / "docs" / "trace.json"))
    args = ap.parse_args()

    store = PostgresTaskStore(args.conninfo)
    ensure_ledger(store)
    conn = psycopg.connect(args.conninfo, row_factory=psycopg.rows.dict_row)
    ensure_naive_ledger(conn)

    with conn.cursor() as cur:
        cur.execute("TRUNCATE tasks, ledger, naive_ledger")
    conn.commit()

    trace = {
        "env": capture_env(conn),
        "amount": AMOUNT,
        "scenarios": [
            scenario_naive(args.conninfo, conn),
            scenario_before_commit(args.conninfo, conn, store),
            scenario_after_commit(args.conninfo, conn, store),
        ],
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(trace, indent=2) + "\n")

    # The page loads this rather than fetching the JSON, so the demo also works
    # when the file is opened straight off disk (file://), not just over http.
    (out.parent / "trace.js").write_text(
        "// Generated by scripts/record_trace.py — a recording of a real run.\n"
        "// The human-readable copy of this data is trace.json, next to it.\n"
        f"window.HAPAX_TRACE = {json.dumps(trace, indent=2)};\n"
    )

    conn.close()
    store.close()

    print(f"wrote {out}")
    for s in trace["scenarios"]:
        mark = "ok " if s["result"]["ok"] else "BAD"
        print(f"  [{mark}] {s['key']:<22} charged ${s['result']['charged']:<4} {s['result']['verdict']}")


if __name__ == "__main__":
    main()
