"""Tests for the TTL reaper loop.

The reaping *logic* is already covered by the store contract suite against both
backends. These tests cover the loop wrapper itself: that it sweeps the right
number of times, deterministically, without real waiting, and reports totals
correctly. Uses the in-memory store so it needs no database.
"""

from hapax.memory import InMemoryTaskStore
from hapax.reaper import reap_once, run_reaper
from hapax.task import TaskState


def _make_expired_terminal_task(store, key):
    t = store.create_task({"op": "x"}, idempotency_key=key, ttl_seconds=-1)
    store.update_task(t.id, TaskState.COMPLETED, result={})
    return t


def test_reap_once_removes_expired_terminal_tasks():
    store = InMemoryTaskStore()
    _make_expired_terminal_task(store, "a")
    _make_expired_terminal_task(store, "b")
    store.create_task({"op": "live"})  # in-flight, must survive

    assert reap_once(store) == 2
    assert len(store.list_tasks()) == 1


def test_run_reaper_bounded_sweeps_do_not_sleep_forever():
    store = InMemoryTaskStore()
    _make_expired_terminal_task(store, "a")

    sleeps: list[float] = []
    swept: list[int] = []

    total = run_reaper(
        store,
        interval_seconds=30.0,
        max_sweeps=3,
        sleep=lambda s: sleeps.append(s),  # never actually waits
        on_sweep=swept.append,
    )

    # 3 sweeps ran; first reaped 1, the next two reaped 0.
    assert swept == [1, 0, 0]
    assert total == 1
    # No trailing sleep after the final sweep: 3 sweeps -> at most 2 sleeps.
    assert len(sleeps) == 2


def test_run_reaper_stops_on_should_stop():
    store = InMemoryTaskStore()
    calls = {"n": 0}

    def should_stop() -> bool:
        # Stop before the 3rd sweep.
        stop = calls["n"] >= 2
        calls["n"] += 1
        return stop

    swept: list[int] = []
    run_reaper(
        store,
        interval_seconds=1.0,
        should_stop=should_stop,
        sleep=lambda s: None,
        on_sweep=swept.append,
    )
    assert len(swept) == 2
