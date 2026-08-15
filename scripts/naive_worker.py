"""The control group: a charge worker written the way most people write it first.

This exists so the comparison in the demo is *fair*. The Hapax worker is crashed
with a real SIGKILL and observed; this one is crashed the same way, in the same
place, against the same database. The only variable is where the "have I already
done this?" bookkeeping lives.

Here it lives in process memory:

    _ALREADY_CHARGED: set[str] = set()

which is the natural thing to write and is correct right up until the process
dies. A retry after a crash is a *new* process with an empty set, so the guard
that was supposed to prevent a double charge is simply gone — and the customer
is charged twice.

That is the whole argument for Hapax in one variable: dedup state has to outlive
the process doing the work, and it has to be enforced by something that cannot
race. Postgres can be that; a Python set cannot.
"""

from __future__ import annotations

import argparse
import os
import signal
import time

import psycopg

NAIVE_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS naive_ledger (
    id         serial      PRIMARY KEY,
    task_id    text        NOT NULL,       -- NOTE: deliberately NOT unique
    amount     integer     NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""

# Process-local memory of work already done. Dies with the process. That is the bug.
_ALREADY_CHARGED: set[str] = set()

CRASH_POINTS = ("before_commit", "after_commit")


def ensure_naive_ledger(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(NAIVE_LEDGER_DDL)
    conn.commit()


def _hard_crash() -> None:
    """Uncatchable kill of this process — the real thing, not an exception."""
    os.kill(os.getpid(), signal.SIGKILL)


def process_charge_naively(
    conninfo: str,
    task_id: str,
    amount: int = 50,
    *,
    crash_at: str | None = None,
    work_seconds: float = 0.0,
) -> str:
    """Charge a customer, guarding against duplicates with an in-memory set."""
    conn = psycopg.connect(conninfo)
    try:
        ensure_naive_ledger(conn)

        # The guard. Empty in every freshly-started process, including the retry
        # that runs after a crash — which is exactly when it is needed.
        if task_id in _ALREADY_CHARGED:
            return "noop-already-charged"

        if work_seconds:
            time.sleep(work_seconds)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO naive_ledger (task_id, amount) VALUES (%s, %s)",
                [task_id, amount],
            )
            if crash_at == "before_commit":
                _hard_crash()

        conn.commit()

        if crash_at == "after_commit":
            # The charge is committed but the in-memory guard was never persisted.
            # A retry will not know this happened.
            _hard_crash()

        _ALREADY_CHARGED.add(task_id)
        return "charged"
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Naive charge worker (crashable control group).")
    ap.add_argument("--conninfo", required=True)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--amount", type=int, default=50)
    ap.add_argument("--crash-at", choices=CRASH_POINTS, default=None)
    ap.add_argument("--work-seconds", type=float, default=0.0)
    args = ap.parse_args()

    print(
        process_charge_naively(
            args.conninfo,
            args.task_id,
            args.amount,
            crash_at=args.crash_at,
            work_seconds=args.work_seconds,
        )
    )


if __name__ == "__main__":
    main()
