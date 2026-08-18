"""Tests for the SEP-2663 wire adapter.

These pin the result *shapes* the protocol layer emits, and — importantly — prove
the durability guarantee survives all the way up at the protocol level: a task
created through the protocol on one store instance is still retrievable, with its
completed result, from a fresh store instance pointed at the same database (i.e.
after a restart).
"""

import os

import pytest

from hapax.errors import TaskNotFound
from hapax.memory import InMemoryTaskStore
from hapax.protocol import TasksProtocol
from hapax.task import TaskState

POSTGRES_URL = os.environ.get("HAPAX_TEST_DATABASE_URL")


@pytest.fixture
def proto():
    return TasksProtocol(InMemoryTaskStore())


# --- shapes -------------------------------------------------------------------


def test_create_returns_task_envelope(proto):
    env = proto.create_augmented("count_rows", {"path": "/data.csv"})
    assert env["resultType"] == "task"
    assert env["status"] == "working"
    assert env["taskId"].startswith("task_")
    assert "createdAt" in env and "lastUpdatedAt" in env
    assert env["ttlMs"] is None  # no ttl set


def test_ttl_is_reported_in_ms(proto):
    env = proto.create_augmented("x", {}, ttl_seconds=60)
    assert env["ttlMs"] is not None
    assert 0 < env["ttlMs"] <= 60_000


def test_get_working_task(proto):
    task_id = proto.create_augmented("x", {})["taskId"]
    got = proto.get(task_id)
    # Always "complete" on a get — it marks the response's own shape, not the
    # task's progress. Only the creation envelope says "task".
    assert got["resultType"] == "complete"
    assert got["status"] == "working"


def test_get_completed_task_inlines_result(proto):
    task_id = proto.create_augmented("x", {})["taskId"]
    proto.store.update_task(task_id, TaskState.COMPLETED, result={"rows": 42})
    got = proto.get(task_id)
    assert got["resultType"] == "complete"
    assert got["status"] == "completed"
    assert got["result"] == {"rows": 42}


def test_get_failed_task_inlines_error(proto):
    task_id = proto.create_augmented("x", {})["taskId"]
    proto.store.update_task(task_id, TaskState.FAILED, error={"code": "upstream_5xx"})
    got = proto.get(task_id)
    assert got["resultType"] == "complete"
    assert got["status"] == "failed"
    assert got["error"] == {"code": "upstream_5xx"}


def test_cancel_returns_empty_ack_and_transitions(proto):
    task_id = proto.create_augmented("x", {})["taskId"]
    assert proto.cancel(task_id) == {}
    assert proto.get(task_id)["status"] == "cancelled"


def test_get_unknown_raises(proto):
    with pytest.raises(TaskNotFound):
        proto.get("task_missing")


# --- idempotency at the protocol level ----------------------------------------


def test_same_key_returns_same_task_id(proto):
    a = proto.create_augmented("charge", {"amt": 50}, idempotency_key="k1")
    b = proto.create_augmented("charge", {"amt": 50}, idempotency_key="k1")
    assert a["taskId"] == b["taskId"]
    assert len(proto.store.list_tasks()) == 1


# --- durability across a restart, at the protocol level -----------------------


@pytest.mark.skipif(not POSTGRES_URL, reason="needs Postgres")
def test_task_survives_restart_through_protocol():
    from hapax.postgres import PostgresTaskStore

    store1 = PostgresTaskStore(POSTGRES_URL)
    with store1._conn.cursor() as cur:
        cur.execute("TRUNCATE tasks")
    store1._conn.commit()

    p1 = TasksProtocol(store1)
    task_id = p1.create_augmented("charge", {"amt": 50}, idempotency_key="restart-1")["taskId"]
    store1.update_task(task_id, TaskState.COMPLETED, result={"charged": 50})
    store1.close()  # simulate the process going away

    # Fresh store instance (a "restart") pointed at the same database.
    store2 = PostgresTaskStore(POSTGRES_URL)
    p2 = TasksProtocol(store2)
    got = p2.get(task_id)
    assert got["status"] == "completed"
    assert got["result"] == {"charged": 50}
    store2.close()
