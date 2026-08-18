"""SEP-2663 wire adapter over the durable store.

This is the seam between the durable `TaskStore` and the MCP Tasks wire protocol.
It turns store operations into the *result shapes* SEP-2663 defines for the
`tasks/*` methods — the exact dicts an MCP server would put on the wire — without
opening any sockets itself. Keeping transport out means it's testable with plain
dictionaries and independent of whichever server framework hosts it.

This targets SEP-2663 as finalized in the 2026-07-28 spec (the
`io.modelcontextprotocol/tasks` extension), which redesigned the experimental
2025-11-25 surface. The differences that matter here:

  * `tasks/get` responses always carry `resultType: "complete"` — it is the
    standard result of the get request itself. Only `CreateTaskResult` (the
    envelope returned in lieu of a tool result) is marked `resultType: "task"`.
  * Cancellation is *cooperative and eventually consistent*: the ack is an
    empty result, and a task may legitimately finish as something other than
    cancelled. Cancelling an already-terminal task is therefore an ack, not an
    error — the work simply finished before the intent arrived.
  * `tasks/list` no longer exists (its authorization scope cannot be defined
    server-side), and blocking `tasks/result` is gone in favour of polling.

One spec requirement Hapax satisfies by construction rather than by effort: a
server MUST NOT return `CreateTaskResult` until the task is durably created,
such that a `tasks/get` for the id would already resolve. Every task here is a
committed Postgres row before the envelope is built.

Not implemented, stated honestly: the `input_required` round trip
(`inputRequests`/`inputResponses`) and `notifications/tasks`. Once the official
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
        """`tasks/cancel`: signal intent, return an empty ack.

        Cancellation is cooperative. A task that already reached a terminal
        state is acked without complaint — the spec is explicit that a
        cancelled task "MAY ultimately reach a terminal status other than
        cancelled if the work finished before cancellation could take effect",
        and an already-finished task is exactly that case. Unknown ids still
        raise (TaskNotFound), which the server maps to a JSON-RPC error.
        """
        task = self.store.get_task(task_id)
        if not task.is_terminal:
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
        result (completed) or error (failed).

        `resultType` is always "complete" here: it marks the shape of the
        response to the *get request*, which is get's own standard result. Only
        the creation envelope is `resultType: "task"` — using "task" for
        in-progress gets was the pre-final reading, and an informed client
        would treat it as the server returning an unexpected CreateTaskResult.
        """
        now = datetime.now(timezone.utc)
        d: dict[str, Any] = {
            "resultType": "complete",
            **self._base(task, now),
        }
        if task.state == TaskState.COMPLETED and task.result is not None:
            d["result"] = task.result
        elif task.state == TaskState.FAILED and task.error is not None:
            d["error"] = task.error
        return d
