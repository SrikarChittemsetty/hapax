"""Crash-recovery / exactly-once tests — the centerpiece.

Each test spawns the worker as a REAL separate OS process, kills it with an
actual SIGKILL at a precise point relative to the commit, and then proves the
side effect (a ledger row) landed exactly once across the crash + recovery.

This is the property the whole project claims. If these pass, the claim is real;
if they can't be made to pass, the claim is a bluff. They only run when a
Postgres URL is configured.
"""

import os
import subprocess
import sys

import pytest

POSTGRES_URL = os.environ.get("HAPAX_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="HAPAX_TEST_DATABASE_URL not set; needs Postgres"
)

# Imports guarded so collection doesn't fail without psycopg installed.
if POSTGRES_URL:
    from hapax.postgres import PostgresTaskStore
    from hapax.task import TaskState
    from hapax.worker import ensure_ledger


def _run_worker(task_id, *, crash_at=None, amount=50, work_seconds=0.0):
    """Spawn the worker as a real subprocess and wait for it to exit."""
    cmd = [
        sys.executable,
        "-m",
        "hapax.worker",
        "--conninfo",
        POSTGRES_URL,
        "--task-id",
        task_id,
        "--amount",
        str(amount),
        "--work-seconds",
        str(work_seconds),
    ]
    if crash_at:
        cmd += ["--crash-at", crash_at]
    return subprocess.run(cmd, capture_output=True, text=True)


@pytest.fixture
def store():
    s = PostgresTaskStore(POSTGRES_URL)
    ensure_ledger(s)
    with s._conn.cursor() as cur:
        cur.execute("TRUNCATE tasks")
        cur.execute("TRUNCATE ledger")
    s._conn.commit()
    try:
        yield s
    finally:
        s.close()


def _ledger_count(store, task_id):
    with store._conn.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM ledger WHERE task_id = %s", [task_id])
        c = cur.fetchone()["c"]
    store._conn.commit()  # end the read txn so later reads get a fresh snapshot
    return c


SIGKILL_RC = -9  # subprocess returncode when the child dies to SIGKILL


def test_crash_before_commit_rolls_back_then_recovers_exactly_once(store):
    task = store.create_task({"op": "charge"}, idempotency_key="c1")

    # Kill mid-transaction, after the ledger INSERT but before commit.
    crashed = _run_worker(task.id, crash_at="before_commit")
    assert crashed.returncode == SIGKILL_RC  # it really was hard-killed

    # The transaction rolled back: task is still WORKING, no charge happened.
    assert store.get_task(task.id).state == TaskState.WORKING
    assert _ledger_count(store, task.id) == 0

    # Recovery: run the worker again, no crash.
    recovered = _run_worker(task.id)
    assert recovered.returncode == 0
    assert "completed" in recovered.stdout

    # Exactly once: task completed, and precisely one ledger row exists.
    assert store.get_task(task.id).state == TaskState.COMPLETED
    assert _ledger_count(store, task.id) == 1


def test_crash_after_commit_is_not_reapplied(store):
    task = store.create_task({"op": "charge"}, idempotency_key="c2")

    # Kill right after the commit: the charge already landed.
    crashed = _run_worker(task.id, crash_at="after_commit")
    assert crashed.returncode == SIGKILL_RC
    assert store.get_task(task.id).state == TaskState.COMPLETED
    assert _ledger_count(store, task.id) == 1

    # Recovery must be a no-op — the task is terminal, so the effect must NOT
    # run a second time.
    recovered = _run_worker(task.id)
    assert recovered.returncode == 0
    assert "noop-terminal" in recovered.stdout
    assert _ledger_count(store, task.id) == 1  # still exactly one


def test_repeated_recovery_stays_exactly_once(store):
    task = store.create_task({"op": "charge"}, idempotency_key="c3")

    # Complete it cleanly once.
    first = _run_worker(task.id)
    assert first.returncode == 0
    assert _ledger_count(store, task.id) == 1

    # Re-running the worker several more times (as a crash-loop might) never
    # adds another charge.
    for _ in range(3):
        again = _run_worker(task.id)
        assert again.returncode == 0
        assert "noop-terminal" in again.stdout
    assert _ledger_count(store, task.id) == 1
