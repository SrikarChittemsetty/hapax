"""SEP-2663 wire adapter over the durable store.

This is the seam between the durable `TaskStore` and the MCP Tasks wire protocol.
It turns store operations into the *result shapes* SEP-2663 defines for the
`tasks/*` methods — the exact dicts an MCP server would put on the wire — without
opening any sockets itself. Keeping transport out means it's testable with plain
dictionaries and independent of whichever server framework hosts it.

Scope, stated honestly: this targets the SEP-2663 wire *fields* as of the final
draft (`resultType`, `taskId`, `status`, `createdAt`, `lastUpdatedAt`, `ttlMs`,
inlined result). It is not a full protocol server (no capability negotiation,
notifications, or the input-required loop — those are roadmap). Once the official
Python SDK's `TaskStore` interface lands (python-sdk #3005), the intent is to
conform to that interface directly; until then this adapter demonstrates the
mapping end to end.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .store import TaskStore
from .task import Task, TaskState


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _ttl_ms(task: Task, now: datetime) -> int | None:
    if task.expires_at is None:
        return None
    return max(0, int((task.expires_at - now).total_seconds() * 1000))


class TasksProtocol:
    """Maps (method, params) to SEP-2663 result dicts, backed by any TaskStore."""

    def __init__(self, store: TaskStore) -> None:
        self.store = store

    # --- methods --------------------------------------------------------------

    def create_augmented(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """A task-augmented `tools/call`: create the task, return the envelope.

        Idempotent on `idempotency_key` (the store guarantees it): a retried call
        returns the same taskId with its current status, never a second task.
        """
        task = self.store.create_task(
            {"tool": tool_name, "arguments": arguments},
            idempotency_key=idempotency_key,
            ttl_seconds=ttl_seconds,
        )
        return self._envelope(task)

    def get(self, task_id: str) -> dict[str, Any]:
        """`tasks/get`: the DetailedTask shape. Raises TaskNotFound for unknown
        ids (a server maps that to a JSON-RPC error)."""
        return self._detailed(self.store.get_task(task_id))

    def cancel(self, task_id: str) -> dict[str, Any]:
        """`tasks/cancel`: transition to cancelled, return an empty ack."""
        self.store.cancel_task(task_id)
        return {}

    # --- shapes ---------------------------------------------------------------

    def _base(self, task: Task, now: datetime) -> dict[str, Any]:
        return {
            "taskId": task.id,
            "status": task.state.value,
            "createdAt": _iso(task.created_at),
            "lastUpdatedAt": _iso(task.updated_at),
            "ttlMs": _ttl_ms(task, now),
        }

    def _envelope(self, task: Task) -> dict[str, Any]:
        """The flat CreateTaskResult returned from an augmented tools/call."""
        now = datetime.now(timezone.utc)
        return {"resultType": "task", **self._base(task, now)}

    def _detailed(self, task: Task) -> dict[str, Any]:
        """The DetailedTask returned from tasks/get. A terminal task inlines its
        result (completed) or error (failed)."""
        now = datetime.now(timezone.utc)
        d: dict[str, Any] = {
            "resultType": "complete" if task.is_terminal else "task",
            **self._base(task, now),
        }
        if task.state == TaskState.COMPLETED and task.result is not None:
            d["result"] = task.result
        elif task.state == TaskState.FAILED and task.error is not None:
            d["error"] = task.error
        return d
