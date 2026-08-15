"""A durable worker that performs a side effect exactly once.

This module exists to be *crashed*. It runs as its own OS process, performs a
"charge" (an INSERT into a ledger table) tied to a task, and can be told to
hard-kill itself (`SIGKILL`, uncatchable — a real `kill -9`) at a precise point
relative to the commit boundary. The crash-recovery tests spawn it, kill it, and
then prove the side effect landed exactly once.

The exactly-once guarantee comes entirely from two things already built:
  * the side effect and the COMPLETED transition commit in ONE transaction
    (`complete_with_effect`), so a crash before commit rolls back both; and
  * COMPLETED is terminal, so a crash *after* commit means recovery sees the
    task is done and refuses to re-run the effect.

The ledger table also has a PRIMARY KEY on task_id as a defence-in-depth
backstop: even a buggy double-run could not insert two charge rows.
"""

from __future__ import annotations

import argparse
import os
import signal
import time

import psycopg

from .postgres import PostgresTaskStore

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS ledger (
    task_id    text        PRIMARY KEY,
    amount     integer     NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""

# Points at which the worker can be told to hard-crash, relative to the commit.
CRASH_POINTS = ("before_effect", "before_commit", "after_commit")


def ensure_ledger(store: PostgresTaskStore) -> None:
    with store._conn.cursor() as cur:
        cur.execute(LEDGER_DDL)
    store._conn.commit()


def _hard_crash() -> None:
    """Uncatchable kill of this process — the real thing, not an exception."""
    os.kill(os.getpid(), signal.SIGKILL)


def process_charge(
    conninfo: str,
    task_id: str,
    amount: int = 50,
    *,
    crash_at: str | None = None,
    work_seconds: float = 0.0,
) -> str:
    """Process a charge task exactly once. Returns an outcome string.

    On recovery (task already terminal) this is a no-op — the whole point.
    """
    store = PostgresTaskStore(conninfo)
    ensure_ledger(store)

    task = store.get_task(task_id)
    if task.is_terminal:
        # Recovery path: the work already finished in a previous (crashed) run.
        return "noop-terminal"

    if crash_at == "before_effect":
        _hard_crash()

    # Simulate slow work so a killer has a window to land mid-flight.
    if work_seconds:
        time.sleep(work_seconds)

    def effect(cur: psycopg.Cursor) -> None:
        cur.execute(
            "INSERT INTO ledger (task_id, amount) VALUES (%s, %s) "
            "ON CONFLICT (task_id) DO NOTHING",
            [task_id, amount],
        )
        if crash_at == "before_commit":
            # Still inside the transaction — nothing has committed. The SIGKILL
            # drops the connection and Postgres rolls the whole transaction back.
            _hard_crash()

    store.complete_with_effect(task_id, effect, result={"charged": amount})

    if crash_at == "after_commit":
        # The transaction committed; the task is COMPLETED and the ledger row
        # exists. Dying here must NOT cause a double charge on recovery.
        _hard_crash()

    return "completed"


def main() -> None:
    ap = argparse.ArgumentParser(description="Durable charge worker (crashable).")
    ap.add_argument("--conninfo", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--amount", type=int, default=50)
    ap.add_argument("--crash-at", choices=CRASH_POINTS, default=None)
    ap.add_argument("--work-seconds", type=float, default=0.0)
    args = ap.parse_args()

    outcome = process_charge(
        args.conninfo,
        args.task_id,
        args.amount,
        crash_at=args.crash_at,
        work_seconds=args.work_seconds,
    )
    print(outcome)


if __name__ == "__main__":
    main()
