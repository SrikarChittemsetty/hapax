"""Concurrency test — two workers race the same task, still exactly once.

Crash recovery proves durability across *time* (a process dies, another picks
up). This proves correctness across *space*: two workers running at the SAME
instant on the SAME task must not both apply the side effect. The `SELECT ...
FOR UPDATE` row lock plus the terminal-state check are what serialize them.

Note the subtle, honest outcome: BOTH workers exit successfully and both think
they finished — but only one actually charged. The lock made the loser's
`complete_with_effect` a no-op. The ledger, not the exit codes, is the source
of truth, and it holds exactly one row.
"""

import os
import subprocess
import sys
import time

import pytest

POSTGRES_URL = os.environ.get("HAPAX_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="HAPAX_TEST_DATABASE_URL not set; needs Postgres"
)

if POSTGRES_URL:
    from hapax.postgres import PostgresTaskStore
    from hapax.task import TaskState
    from hapax.worker import ensure_ledger


def _spawn_worker(task_id, *, work_seconds):
    """Start a worker WITHOUT waiting — so two can overlap in time."""
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hapax.worker",
            "--conninfo",
            POSTGRES_URL,
            "--task-id",
            task_id,
            "--work-seconds",
            str(work_seconds),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


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
    store._conn.commit()
    return c


def test_two_workers_race_same_task_charge_once(store):
    task = store.create_task({"op": "charge"}, idempotency_key="race-1")

    # A small work window widens the overlap so the race is real, not theoretical.
    a = _spawn_worker(task.id, work_seconds=0.3)
    b = _spawn_worker(task.id, work_seconds=0.3)

    a.wait(timeout=30)
    b.wait(timeout=30)

    # Both processes finished cleanly...
    assert a.returncode == 0
    assert b.returncode == 0

    # ...but the charge happened exactly once, and the task is completed.
    assert store.get_task(task.id).state == TaskState.COMPLETED
    assert _ledger_count(store, task.id) == 1


def test_many_stores_can_apply_the_schema_at_once(store):
    """Every store applies the schema on connect, so startup is itself a race.

    This is a regression test for a real deadlock: adding a column to the schema
    made `apply_schema` take an AccessExclusiveLock on `tasks` even when there
    was nothing to add, and two workers starting together deadlocked on it. The
    column is dropped here to force the migration path, which is the only path
    that can deadlock.
    """
    import threading

    with store._conn.cursor() as cur:
        cur.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS lease_expires_at")
    store._conn.commit()

    errors: list[Exception] = []

    def connect_and_migrate():
        try:
            PostgresTaskStore(POSTGRES_URL).close()
        except Exception as e:  # noqa: BLE001 — the failure is the whole point
            errors.append(e)

    threads = [threading.Thread(target=connect_and_migrate) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent schema application failed: {errors[:2]}"

    # And the migration actually happened, rather than everyone quietly skipping it.
    with store._conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns"
            " WHERE table_name = 'tasks' AND column_name = 'lease_expires_at'"
        )
        assert cur.fetchone() is not None
    store._conn.commit()
