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

# An arbitrary but fixed key for the advisory lock that serializes schema
# migration. Advisory locks are just int64s Postgres tracks for you; the value
# means nothing beyond "everyone migrating this schema agrees to use this one".
_SCHEMA_LOCK_KEY = 0x48415041  # "HAPA"

# The terminal states, as bare strings, for use in SQL IN-clauses.
_TERMINAL_VALUES = tuple(s.value for s in TERMINAL_STATES)

_COLUMNS = (
    "id, state, input, idempotency_key, result, error, "
    "progress, progress_message, created_at, updated_at, expires_at"
)
# Reads also pull the lease bookkeeping. Writes don't: create_task never sets a
# lease (a new task is unclaimed by definition) and the lease columns have
# defaults, so keeping the insert list narrow avoids restating them everywhere.
_READ_COLUMNS = _COLUMNS + ", attempts, lease_expires_at"


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
        # .get(): rows coming back from the INSERT ... RETURNING in create_task
        # use the narrow column list and carry no lease fields yet.
        attempts=row.get("attempts", 0) or 0,
        lease_expires_at=row.get("lease_expires_at"),
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
        """Bring the database up to the current schema, safely under concurrency.

        Every store applies the schema on connect, which means N workers starting
        at once all try to run the same DDL. That is fine for `CREATE ... IF NOT
        EXISTS`, which does nothing when the object is there, but *not* for
        `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`: it takes an AccessExclusiveLock
        on the table before it can decide there is nothing to do. Two workers
        doing that simultaneously deadlock each other — which is exactly how this
        was found, by the two-workers-racing test starting to fail.

        So: check first, and only reach for DDL if the schema is actually behind.
        The common path — an already-migrated database — takes no locks at all.
        """
        with self._conn.cursor() as cur:
            if self._schema_is_current(cur):
                self._conn.commit()
                return

            # Serialize the processes that *do* need to migrate, so they queue
            # for the DDL rather than deadlocking over it. The lock is held for
            # the transaction and released by the commit below.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [_SCHEMA_LOCK_KEY])
            # Re-check: while waiting for the lock, another process may have
            # done the work already.
            if not self._schema_is_current(cur):
                cur.execute(_SCHEMA_SQL)
        self._conn.commit()

    @staticmethod
    def _schema_is_current(cur: psycopg.Cursor[dict[str, Any]]) -> bool:
        """Is the tasks table present and carrying the newest columns?"""
        cur.execute(
            """
            SELECT to_regclass('tasks') IS NOT NULL
                   AND EXISTS (
                       SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'tasks' AND column_name = 'lease_expires_at'
                   ) AS ready
            """
        )
        row = cur.fetchone()
        return bool(row["ready"]) if row else False

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
                    f"SELECT {_READ_COLUMNS} FROM tasks WHERE idempotency_key = %s",
                    [idempotency_key],
                )
                existing = cur.fetchone()
                # existing is guaranteed present: the conflict means the row is there.
                assert existing is not None
                return _row_to_task(existing)

    # --- leases: claiming work, and noticing when a claimant dies -------------

    def claim_task(self, *, lease_seconds: float = 30) -> Task | None:
        """Take exclusive ownership of one claimable task, or return None.

        A task is claimable if it is still `working` and nobody holds a live
        lease on it — either it has never been claimed, or the worker that
        claimed it stopped renewing (which, for a process that took a SIGKILL,
        means it stopped existing).

        The whole thing is one statement on purpose. A SELECT to find a
        candidate followed by an UPDATE to claim it is the same check-then-act
        race that `create_task` avoids at the other end of the lifecycle: two
        dispatchers would both see the same expired task and both claim it.
        Here the subquery takes a row lock and the UPDATE writes the lease
        inside the same statement, so exactly one claimant can win.

        `SKIP LOCKED` is what makes this usable with more than one dispatcher:
        a claimant that finds a row already locked steps over it and takes the
        next one, instead of blocking until the other transaction commits.
        """
        now = _now()
        deadline = now + timedelta(seconds=lease_seconds)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE tasks
                       SET lease_expires_at = %(deadline)s,
                           attempts = attempts + 1
                     WHERE id = (
                           SELECT id FROM tasks
                            WHERE state = 'working'
                              AND (lease_expires_at IS NULL OR lease_expires_at < %(now)s)
                            ORDER BY created_at
                              FOR UPDATE SKIP LOCKED
                            LIMIT 1
                           )
                    RETURNING {_READ_COLUMNS}
                    """,
                    {"deadline": deadline, "now": now},
                )
                row = cur.fetchone()
        return _row_to_task(row) if row is not None else None

    def heartbeat(self, task_id: str, *, lease_seconds: float = 30) -> bool:
        """Push a held lease further out. Returns False if the task is gone or
        already terminal — a worker whose task went terminal underneath it
        (cancelled, say) learns that here rather than by finishing work nobody
        wants any more."""
        deadline = _now() + timedelta(seconds=lease_seconds)
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE tasks SET lease_expires_at = %s"
                    " WHERE id = %s AND state = 'working' RETURNING id",
                    [deadline, task_id],
                )
                return cur.fetchone() is not None

    def count_claimable(self) -> int:
        """How many tasks are sitting there waiting for someone to pick up.

        Useful as a health metric: a number that climbs means work is arriving
        faster than dispatchers can take it, or that something is repeatedly
        claiming and dying.
        """
        now = _now()
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM tasks WHERE state = 'working'"
                " AND (lease_expires_at IS NULL OR lease_expires_at < %s)",
                [now],
            )
            row = cur.fetchone()
        self._conn.commit()
        return int(row["n"]) if row else 0

    # --- read -----------------------------------------------------------------

    def get_task(self, task_id: str) -> Task:
        with self._conn.cursor() as cur:
            cur.execute(
                f"SELECT {_READ_COLUMNS} FROM tasks WHERE id = %s", [task_id]
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
                    f"SELECT {_READ_COLUMNS} FROM tasks WHERE id = %s FOR UPDATE",
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
                    f"SELECT {_READ_COLUMNS} FROM tasks WHERE id = %s FOR UPDATE",
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
