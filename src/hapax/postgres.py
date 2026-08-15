"""Postgres-backed TaskStore — the durable implementation.

This is the whole point of the project: the same TaskStore contract as the
in-memory backend, but state lives in Postgres, so it survives a process crash.

Two decisions carry the design, and both are deliberately pushed down into the
database rather than done in Python, because Python-level checks race under
concurrency and don't survive a crash mid-check:

  1. Idempotent create -> INSERT ... ON CONFLICT (idempotency_key) DO NOTHING.
     The UNIQUE index is the source of truth. Even if two requests with the same
     key arrive simultaneously, exactly one row is created; the other request
     falls through to a SELECT and gets the winner. No check-then-insert race.

  2. Safe update -> SELECT ... FOR UPDATE inside a transaction. The row is locked
     for the duration of the read-modify-write, so two concurrent updaters can't
     both read the old state and stomp each other. If the state-machine check
     rejects the transition, the transaction rolls back and nothing persists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .errors import TaskNotFound
from .task import TERMINAL_STATES, Task, TaskState, _now
from .store import new_task_id

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()

# The terminal states, as bare strings, for use in SQL IN-clauses.
_TERMINAL_VALUES = tuple(s.value for s in TERMINAL_STATES)

_COLUMNS = (
    "id, state, input, idempotency_key, result, error, "
    "progress, progress_message, created_at, updated_at, expires_at"
)


def _row_to_task(row: dict[str, Any]) -> Task:
    """Map a DB row (dict) back into a Task. jsonb columns come back as dicts
    already, so no manual JSON parsing is needed."""
    return Task(
        id=row["id"],
        state=TaskState(row["state"]),
        input=row["input"] or {},
        idempotency_key=row["idempotency_key"],
        result=row["result"],
        error=row["error"],
        progress=row["progress"],
        progress_message=row["progress_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


class PostgresTaskStore:
    """A durable TaskStore. Conforms structurally to the TaskStore Protocol.

    Holds one connection. Across processes (e.g. the fault-injection worker),
    each process opens its own store/connection and Postgres arbitrates via the
    row locks — that's how correctness holds up under a real crash, not just
    in-process.
    """

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        # autocommit=False (the default): we manage transactions explicitly so
        # the read-modify-write in update_task is atomic.
        self._conn = psycopg.connect(conninfo, row_factory=dict_row)
        self.apply_schema()

    def apply_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- create (idempotent) --------------------------------------------------

    def create_task(
        self,
        input: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        ttl_seconds: int | None = None,
    ) -> Task:
        now = _now()
        expires_at = None
        if ttl_seconds is not None:
            expires_at = now + timedelta(seconds=ttl_seconds)

        task = Task(
            id=new_task_id(),
            state=TaskState.WORKING,
            input=dict(input),
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )

        with self._conn.transaction():
            with self._conn.cursor() as cur:
                # Try to insert. If a row with this idempotency_key already
                # exists, ON CONFLICT DO NOTHING makes the insert a no-op and
                # RETURNING yields no row.
                cur.execute(
                    f"""
                    INSERT INTO tasks ({_COLUMNS})
                    VALUES (
                        %(id)s, %(state)s, %(input)s, %(idempotency_key)s,
                        %(result)s, %(error)s, %(progress)s, %(progress_message)s,
                        %(created_at)s, %(updated_at)s, %(expires_at)s
                    )
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING {_COLUMNS}
                    """,
                    {
                        "id": task.id,
                        "state": task.state.value,
                        "input": Jsonb(task.input),
                        "idempotency_key": task.idempotency_key,
                        "result": Jsonb(task.result) if task.result is not None else None,
                        "error": Jsonb(task.error) if task.error is not None else None,
                        "progress": task.progress,
                        "progress_message": task.progress_message,
                        "created_at": task.created_at,
                        "updated_at": task.updated_at,
                        "expires_at": task.expires_at,
                    },
                )
                row = cur.fetchone()
                if row is not None:
                    return _row_to_task(row)

                # Conflict: a task with this key already exists. Return it.
                cur.execute(
                    f"SELECT {_COLUMNS} FROM tasks WHERE idempotency_key = %s",
                    [idempotency_key],
                )
                existing = cur.fetchone()
                # existing is guaranteed present: the conflict means the row is there.
                assert existing is not None
                return _row_to_task(existing)

    # --- read -----------------------------------------------------------------

    def get_task(self, task_id: str) -> Task:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM tasks WHERE id = %s", [task_id]
            )
            row = cur.fetchone()
        self._conn.commit()
        if row is None:
            raise TaskNotFound(task_id)
        return _row_to_task(row)

    # --- update (guarded, row-locked) ----------------------------------------

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
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                # Lock the row for the read-modify-write. Any concurrent updater
                # blocks here until we commit.
                cur.execute(
                    f"SELECT {_COLUMNS} FROM tasks WHERE id = %s FOR UPDATE",
                    [task_id],
                )
                row = cur.fetchone()
                if row is None:
                    raise TaskNotFound(task_id)

                current = _row_to_task(row)
                # State-machine check. Illegal transition -> raises -> the
                # `with self._conn.transaction()` block rolls back -> no write.
                moved = current.transition_to(
                    new_state,
                    result=result,
                    error=error,
                    progress=progress,
                    progress_message=progress_message,
                )

                cur.execute(
                    """
                    UPDATE tasks
                       SET state = %(state)s,
                           result = %(result)s,
                           error = %(error)s,
                           progress = %(progress)s,
                           progress_message = %(progress_message)s,
                           updated_at = %(updated_at)s
                     WHERE id = %(id)s
                    """,
                    {
                        "id": moved.id,
                        "state": moved.state.value,
                        "result": Jsonb(moved.result) if moved.result is not None else None,
                        "error": Jsonb(moved.error) if moved.error is not None else None,
                        "progress": moved.progress,
                        "progress_message": moved.progress_message,
                        "updated_at": moved.updated_at,
                    },
                )
                return moved

    def cancel_task(self, task_id: str) -> Task:
        return self.update_task(task_id, TaskState.CANCELLED)

    # --- atomic side-effect + completion (the exactly-once primitive) ---------

    def complete_with_effect(
        self,
        task_id: str,
        effect: Callable[[psycopg.Cursor[Any]], None],
        *,
        result: dict[str, Any] | None = None,
    ) -> Task:
        """Apply `effect` and move the task to COMPLETED in ONE transaction.

        This is how you get *true* exactly-once for a side effect that lives in
        this same database: the effect (e.g. an INSERT into a ledger) and the
        state transition commit together or not at all. A crash before commit
        rolls back both — the task stays WORKING and a retry re-runs cleanly. A
        crash after commit leaves the task COMPLETED, and because COMPLETED is
        terminal, recovery sees it's done and never re-applies the effect.

        Already-completed tasks are a no-op (returns the existing task), so
        calling this twice is safe — the second call does nothing.

        For an *external* side effect (a Stripe call that can't join this
        transaction) you can't get true exactly-once; you get at-least-once plus
        an idempotency key at the external boundary = effectively-once. That
        honest distinction is the point of the `idempotency_key` column.
        """
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM tasks WHERE id = %s FOR UPDATE",
                    [task_id],
                )
                row = cur.fetchone()
                if row is None:
                    raise TaskNotFound(task_id)

                current = _row_to_task(row)
                if current.state == TaskState.COMPLETED:
                    # Idempotent recovery: the effect already committed. Do not
                    # run it again.
                    return current

                # Validate the transition before doing the effect.
                moved = current.transition_to(TaskState.COMPLETED, result=result)

                # The side effect, in the same transaction as the state change.
                effect(cur)

                cur.execute(
                    """
                    UPDATE tasks
                       SET state = %(state)s,
                           result = %(result)s,
                           updated_at = %(updated_at)s
                     WHERE id = %(id)s
                    """,
                    {
                        "id": moved.id,
                        "state": moved.state.value,
                        "result": Jsonb(moved.result) if moved.result is not None else None,
                        "updated_at": moved.updated_at,
                    },
                )
                return moved

    # --- list / reap ----------------------------------------------------------

    def list_tasks(self) -> list[Task]:
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM tasks")
            rows = cur.fetchall()
        self._conn.commit()
        return [_row_to_task(r) for r in rows]

    def reap_expired(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM tasks
                     WHERE state = ANY(%s)
                       AND expires_at IS NOT NULL
                       AND expires_at <= %s
                    """,
                    [list(_TERMINAL_VALUES), now],
                )
                return cur.rowcount
