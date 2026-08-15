"""In-memory TaskStore — the reference implementation, no persistence.

This is deliberately the *simple* backend: two dicts and nothing else. It exists
to (a) pin down the exact semantics every backend must match, and (b) serve as
the control in the benchmark later. It does NOT survive a restart — that's the
whole point of the Postgres backend that comes next.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import TaskNotFound
from .task import Task, TaskState, _now
from .store import new_task_id


class InMemoryTaskStore:
    """A TaskStore backed by plain dictionaries.

    Structurally conforms to the TaskStore Protocol (no explicit subclassing).
    """

    def __init__(self) -> None:
        # task_id -> Task
        self._tasks: dict[str, Task] = {}
        # idempotency_key -> task_id. This secondary index is what makes dedup
        # an O(1) lookup instead of a scan.
        self._by_key: dict[str, str] = {}
        # task_id -> lease deadline, and task_id -> times claimed. Separate from
        # Task itself because a lease says something about the *worker* holding
        # the task, not about the task.
        self._leases: dict[str, datetime] = {}
        self._attempts: dict[str, int] = {}

    def create_task(
        self,
        input: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        ttl_seconds: int | None = None,
    ) -> Task:
        # --- idempotency: same key -> same task, no second creation ----------
        if idempotency_key is not None:
            existing_id = self._by_key.get(idempotency_key)
            if existing_id is not None:
                # Return the task that already exists. The caller gets the same
                # handle it got last time; no new task, no repeated side effect.
                return self._tasks[existing_id]

        expires_at = None
        if ttl_seconds is not None:
            expires_at = _now() + timedelta(seconds=ttl_seconds)

        task = Task(
            id=new_task_id(),
            state=TaskState.WORKING,
            input=dict(input),
            idempotency_key=idempotency_key,
            expires_at=expires_at,
        )
        self._tasks[task.id] = task
        if idempotency_key is not None:
            self._by_key[idempotency_key] = task.id
        return task

    def get_task(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise TaskNotFound(task_id) from None

    def update_task(
        self,
        task_id: str,
        new_state: TaskState,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        progress: float | None = None,
        progress_message: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        # transition_to() enforces the state machine; an illegal move raises
        # here before anything is persisted.
        moved = task.transition_to(
            new_state,
            result=result,
            error=error,
            progress=progress,
            progress_message=progress_message,
        )
        self._tasks[task_id] = moved
        return moved

    # --- leases ---------------------------------------------------------------
    #
    # Same semantics as the Postgres backend so the contract suite can hold both
    # to one standard. There is no crash story here — a process dying takes this
    # whole store with it — but the *rules* about what is claimable have to match,
    # or the contract tests would be testing two different things.

    def claim_task(self, *, lease_seconds: float = 30) -> Task | None:
        now = _now()
        claimable = [
            t
            for t in self._tasks.values()
            if t.state is TaskState.WORKING
            and (self._leases.get(t.id) is None or self._leases[t.id] < now)
        ]
        if not claimable:
            return None
        # Oldest first, matching the Postgres ORDER BY created_at.
        task = min(claimable, key=lambda t: (t.created_at, t.id))
        deadline = now + timedelta(seconds=lease_seconds)
        self._leases[task.id] = deadline
        self._attempts[task.id] = self._attempts.get(task.id, 0) + 1
        claimed = replace(
            task, attempts=self._attempts[task.id], lease_expires_at=deadline
        )
        self._tasks[task.id] = claimed
        return claimed

    def heartbeat(self, task_id: str, *, lease_seconds: float = 30) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.state is not TaskState.WORKING:
            return False
        self._leases[task_id] = _now() + timedelta(seconds=lease_seconds)
        return True

    def count_claimable(self) -> int:
        now = _now()
        return sum(
            1
            for t in self._tasks.values()
            if t.state is TaskState.WORKING
            and (self._leases.get(t.id) is None or self._leases[t.id] < now)
        )

    def cancel_task(self, task_id: str) -> Task:
        return self.update_task(task_id, TaskState.CANCELLED)

    def list_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def reap_expired(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        to_remove = [
            t.id
            for t in self._tasks.values()
            if t.is_terminal and t.expires_at is not None and t.expires_at <= now
        ]
        for task_id in to_remove:
            task = self._tasks.pop(task_id)
            if task.idempotency_key is not None:
                # Keep the secondary index consistent with the primary store.
                self._by_key.pop(task.idempotency_key, None)
        return len(to_remove)
