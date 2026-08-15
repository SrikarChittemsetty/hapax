"""The TaskStore interface — the contract every backend must satisfy.

The MCP reference SDKs define a store interface and ship only an in-memory
implementation of it, explicitly leaving a persistent one to you. This is that
interface, expressed as a `typing.Protocol` so any class with the right methods
(in-memory, Postgres, Redis, …) conforms *structurally* — no explicit
inheritance required. The whole project is: implement this contract in a way
that survives a crash.

Every method's semantics are written here on purpose, because they are the
spec the backends are tested against.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

from .task import Task, TaskState


def new_task_id() -> str:
    """Opaque, collision-resistant task id. Callers treat it as a handle."""
    return "task_" + uuid.uuid4().hex


@runtime_checkable
class TaskStore(Protocol):
    """Persistence contract for tasks.

    A conforming store guarantees:
      * create_task is **idempotent** on idempotency_key — calling it twice with
        the same key returns the same task and never creates a second one.
      * update_task applies transitions through the Task state machine, so an
        illegal transition raises InvalidTransition rather than corrupting state.
      * get_task raises TaskNotFound for unknown ids.
    """

    def create_task(
        self,
        input: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        ttl_seconds: int | None = None,
    ) -> Task:
        """Create a new WORKING task, or return the existing one if a task with
        this idempotency_key already exists. This dedup is what prevents a
        retried request from kicking off the underlying work a second time."""
        ...

    def get_task(self, task_id: str) -> Task:
        """Return the task, or raise TaskNotFound."""
        ...

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
        """Apply a guarded state transition and persist it. Raises TaskNotFound
        for unknown ids and InvalidTransition for illegal moves."""
        ...

    def cancel_task(self, task_id: str) -> Task:
        """Convenience for update_task(task_id, CANCELLED)."""
        ...

    def list_tasks(self) -> list[Task]:
        """All tasks currently in the store (unordered)."""
        ...

    def reap_expired(self, *, now: Any = None) -> int:
        """Delete terminal tasks whose expires_at has passed. Returns the count
        removed. Only terminal tasks are eligible — in-flight work is never
        reaped out from under a caller."""
        ...
