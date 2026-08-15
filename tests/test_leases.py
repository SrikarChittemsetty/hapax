"""Leases, claiming, and automatic recovery of tasks whose worker died.

The crash-recovery tests prove that *if* something re-runs a task, the charge
still happens exactly once. These prove the "if" away: nothing outside the
system has to notice the crash or ask for a retry.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time

import pytest

from hapax.dispatcher import Dispatcher
from hapax.memory import InMemoryTaskStore
from hapax.task import TaskState
from hapax.worker import process_charge


# --- contract: both backends must agree on what "claimable" means ------------


def test_claim_returns_a_working_task(store):
    created = store.create_task({"op": "charge"})
    claimed = store.claim_task(lease_seconds=30)
    assert claimed is not None
    assert claimed.id == created.id
    assert claimed.attempts == 1


def test_a_live_lease_makes_the_task_unclaimable(store):
    store.create_task({"op": "charge"})
    assert store.claim_task(lease_seconds=30) is not None
    # Someone already holds it, so there is nothing left to pick up.
    assert store.claim_task(lease_seconds=30) is None


def test_an_expired_lease_makes_it_claimable_again(store):
    created = store.create_task({"op": "charge"})
    first = store.claim_task(lease_seconds=0)  # already expired on arrival
    assert first is not None

    second = store.claim_task(lease_seconds=30)
    assert second is not None
    assert second.id == created.id
    # The count is what tells a dispatcher this task keeps coming back.
    assert second.attempts == 2


def test_terminal_tasks_are_never_claimable(store):
    task = store.create_task({"op": "charge"})
    store.update_task(task.id, TaskState.COMPLETED, result={"charged": 50})
    assert store.claim_task(lease_seconds=0) is None
    assert store.count_claimable() == 0


def test_heartbeat_extends_a_lease_and_reports_terminal_tasks(store):
    task = store.create_task({"op": "charge"})
    store.claim_task(lease_seconds=0)

    assert store.heartbeat(task.id, lease_seconds=60) is True
    # Renewed, so it is no longer up for grabs even though the first lease lapsed.
    assert store.claim_task(lease_seconds=30) is None

    store.update_task(task.id, TaskState.COMPLETED, result={"charged": 50})
    # A worker still plugging away on a task that went terminal finds out here.
    assert store.heartbeat(task.id, lease_seconds=60) is False


def test_claim_hands_each_task_to_exactly_one_caller(store):
    ids = {store.create_task({"i": i}).id for i in range(5)}
    claimed = [store.claim_task(lease_seconds=30) for _ in range(5)]
    assert all(c is not None for c in claimed)
    # No task handed out twice — the point of doing the claim in one statement.
    assert {c.id for c in claimed} == ids
    assert store.claim_task(lease_seconds=30) is None


def test_count_claimable_tracks_the_backlog(store):
    for i in range(3):
        store.create_task({"i": i})
    assert store.count_claimable() == 3
    store.claim_task(lease_seconds=30)
    assert store.count_claimable() == 2


# --- two dispatchers racing the same expired task ----------------------------


def test_two_stores_racing_one_expired_task(pg_store, pg_conninfo):
    """Postgres only: the race needs two separate connections.

    Both dispatchers see the same lapsed task at the same moment. SKIP LOCKED
    means the loser steps over the locked row rather than blocking on it, and
    exactly one of them comes away with the task.
    """
    from hapax.postgres import PostgresTaskStore

    pg_store.create_task({"op": "charge"})
    pg_store.claim_task(lease_seconds=0)  # lease lapses immediately

    other = PostgresTaskStore(pg_conninfo)
    try:
        a = pg_store.claim_task(lease_seconds=30)
        b = other.claim_task(lease_seconds=30)
        assert [a, b].count(None) == 1, "exactly one claimant should win"
    finally:
        other.close()


# --- the one that matters: nobody retries, and it still recovers -------------


def test_a_killed_worker_is_recovered_without_anyone_asking(pg_store, pg_conninfo, ledger):
    """Kill a worker mid-charge. Do not retry it. The system recovers anyway.

    This is the difference between the store recovering *when asked* and the
    system recovering on its own, and it is the whole reason leases exist. The
    lease here is one second, so the test can watch it lapse in real time
    instead of faking the clock.
    """
    task = pg_store.create_task({"op": "charge"}, idempotency_key="orphan-1")

    proc = subprocess.Popen(
        [sys.executable, "-m", "hapax.worker", "--conninfo", pg_conninfo,
         "--task-id", task.id, "--work-seconds", "5", "--lease-seconds", "1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(0.6)          # let it start, take the lease, and begin the work
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)

    # Nothing has told the system anything. The task looks exactly like one
    # being worked on right now — that is precisely the problem leases solve.
    assert pg_store.get_task(task.id).state is TaskState.WORKING
    assert ledger.total(task.id) == 0

    dispatcher = Dispatcher(
        store=pg_store,
        handler=lambda t: process_charge(pg_conninfo, t.id),
        lease_seconds=30,
    )

    # While the dead worker's lease is still live, the system correctly refuses
    # to hand the task to anyone else. It cannot yet tell death from slowness,
    # and guessing would be how you get two workers on one payment.
    assert dispatcher.sweep(limit=10) == 0
    assert ledger.total(task.id) == 0

    time.sleep(1.0)          # the lease lapses; nobody renewed it

    # Now the task is claimable again, and a sweep picks it up with no external
    # retry involved at any point.
    assert dispatcher.sweep(limit=10) == 1

    assert pg_store.get_task(task.id).state is TaskState.COMPLETED
    assert ledger.rows(task.id) == 1
    assert ledger.total(task.id) == 50


def test_redispatching_a_finished_task_does_not_charge_twice(pg_store, pg_conninfo, ledger):
    """The dispatcher is allowed to be wrong about who is alive.

    A worker that committed and then died leaves a completed task. Because
    terminal tasks are not claimable, a sweep finds nothing — and even if the
    handler is called by hand, it declines.
    """
    task = pg_store.create_task({"op": "charge"}, idempotency_key="orphan-2")
    process_charge(pg_conninfo, task.id)
    assert ledger.total(task.id) == 50

    dispatcher = Dispatcher(
        store=pg_store,
        handler=lambda t: process_charge(pg_conninfo, t.id),
        lease_seconds=0,
    )
    assert dispatcher.sweep(limit=10) == 0
    assert process_charge(pg_conninfo, task.id) == "noop-terminal"
    assert ledger.rows(task.id) == 1


def test_a_task_that_keeps_killing_workers_is_eventually_abandoned():
    """Without this, one poisonous task takes down every worker, for ever."""
    store = InMemoryTaskStore()
    store.create_task({"op": "charge"}, idempotency_key="poison")

    def explode(task):
        raise RuntimeError("this task kills whatever picks it up")

    dispatcher = Dispatcher(store=store, handler=explode, lease_seconds=0, max_attempts=3)
    for _ in range(6):
        dispatcher.sweep(limit=1)

    assert dispatcher.stats.failed == 3, "tried, and stopped trying"
    assert dispatcher.stats.abandoned >= 1
    # Left in place rather than deleted: someone should look at it.
    assert store.get_task(store.list_tasks()[0].id).state is TaskState.WORKING
