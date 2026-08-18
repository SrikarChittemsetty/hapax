"""A TaskStore that reports on itself.

`InstrumentedTaskStore` wraps any conforming store and emits metrics and spans
around it. It is a wrapper rather than instrumentation edited into
`postgres.py` for three reasons:

  * `TaskStore` is a `typing.Protocol`, so conformance is structural — a wrapper
    that forwards the right methods *is* a TaskStore, with no inheritance and no
    change to the thing being wrapped.
  * The backends stay dependency-free and stay readable. The durability logic in
    `postgres.py` is the part worth reviewing, and threading metric calls
    through it would bury the SQL in bookkeeping.
  * It works for `MemoryTaskStore` too, so the in-memory control group in the
    benchmarks is measured by exactly the same code.

    store = InstrumentedTaskStore(PostgresTaskStore(conninfo))

Anything not explicitly instrumented is forwarded untouched, so the wrapper does
not have to be updated when the store grows a method.
"""

from __future__ import annotations

from typing import Any

from . import telemetry
from .task import Task, TaskState


class InstrumentedTaskStore:
    """Telemetry decorator over a TaskStore."""

    def __init__(self, inner: Any, instruments: telemetry.Instruments | None = None) -> None:
        """`instruments` is injectable so a test can supply an in-memory reader
        instead of going through the process-global MeterProvider."""
        self._inner = inner
        self._m = instruments if instruments is not None else telemetry.instruments()

    # -- forwarding ---------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Forward backend-specific extras — `apply_schema`, `close`, and so on.

        Only called for attributes missing on this class, so the explicit
        methods below always win. `_inner` is set in __init__ and therefore
        found normally — this does not recurse.

        Note that this is NOT enough to satisfy the TaskStore Protocol. A
        `runtime_checkable` isinstance check uses `inspect.getattr_static`,
        which walks the class dictionary and deliberately bypasses
        `__getattr__` — so a method that exists only by forwarding is invisible
        to it. Every method in the contract is therefore written out below,
        even the ones that do nothing but delegate.
        """
        return getattr(self._inner, name)

    # -- contract methods that only delegate --------------------------------
    #
    # Pure forwarding, but explicit for the reason in __getattr__ above: these
    # have to exist on the class for `isinstance(store, TaskStore)` to hold.

    def get_task(self, task_id: str) -> Task:
        return self._inner.get_task(task_id)

    def count_claimable(self) -> int:
        return int(self._inner.count_claimable())

    def cancel_task(self, task_id: str) -> Task:
        return self._inner.cancel_task(task_id)

    def list_tasks(self) -> list[Task]:
        return list(self._inner.list_tasks())

    def reap_expired(self, *, now: Any = None) -> int:
        return int(self._inner.reap_expired(now=now))

    @property
    def inner(self) -> Any:
        """The wrapped store, for tests that need to reach past the decorator."""
        return self._inner

    # -- instrumented paths -------------------------------------------------

    def create_task(
        self,
        input: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        ttl_seconds: int | None = None,
    ) -> Task:
        with telemetry.span(
            "hapax.create_task",
            **{"hapax.idempotent": idempotency_key is not None},
        ) as current:
            task = self._inner.create_task(
                input, idempotency_key=idempotency_key, ttl_seconds=ttl_seconds
            )
            if current is not None:
                # Free on a span, ruinous on a metric — see telemetry.attrs.
                current.set_attribute("hapax.task_id", task.id)

            self._m.created.add(
                1, telemetry.attrs(task_id=task.id, idempotent=idempotency_key is not None)
            )
            return task

    def claim_task(self, *, lease_seconds: float = 30) -> Task | None:
        attributes = telemetry.attrs()
        with telemetry.span("hapax.claim_task") as current:
            with telemetry.timed(self._m.claim_duration, attributes):
                task = self._inner.claim_task(lease_seconds=lease_seconds)

            if task is None:
                if current is not None:
                    current.set_attribute("hapax.claimed", False)
                return None

            # attempts is incremented by the claim itself, so anything above 1
            # means a previous holder took this task and never finished it —
            # the lease lapsed and this claim is a recovery. No crash had to be
            # detected or reported for this to be true, which is the whole
            # point of the lease design.
            recovered = task.attempts > 1

            if current is not None:
                current.set_attribute("hapax.claimed", True)
                current.set_attribute("hapax.task_id", task.id)
                current.set_attribute("hapax.attempts", task.attempts)
                current.set_attribute("hapax.recovered", recovered)

            self._m.claimed.add(1, telemetry.attrs(task_id=task.id))
            if recovered:
                self._m.recovered.add(1, telemetry.attrs(task_id=task.id))
            return task

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
        with telemetry.span(
            "hapax.update_task",
            **{"hapax.task_id": task_id, "hapax.to_state": str(new_state.value)},
        ):
            task = self._inner.update_task(
                task_id,
                new_state,
                result=result,
                error=error,
                progress=progress,
                progress_message=progress_message,
            )
            # Only the destination state is recorded. Capturing from_state would
            # mean reading the row before writing it — a second round trip on
            # every transition, to label a counter. The state machine already
            # constrains which transitions are legal, so the destination plus
            # the totals is enough to reconstruct the flow.
            self._m.transitions.add(
                1, telemetry.attrs(task_id=task_id, to_state=str(new_state.value))
            )
            return task

    def complete_with_effect(self, *args: Any, **kwargs: Any) -> Any:
        """The exactly-once path: side effect and terminal state in one commit.

        Signature-agnostic on purpose — this forwards whatever the backend
        accepts, so the wrapper does not break if the effect API changes.
        """
        with telemetry.span("hapax.complete_with_effect"):
            task = self._inner.complete_with_effect(*args, **kwargs)
            state = getattr(task, "state", None)
            self._m.transitions.add(
                1,
                telemetry.attrs(
                    task_id=getattr(task, "id", None),
                    to_state=str(state.value) if state is not None else None,
                    with_effect=True,
                ),
            )
            return task

    def heartbeat(self, task_id: str, *, lease_seconds: float = 30) -> bool:
        # No metric. A heartbeat fires several times per task per lease period,
        # so counting them measures the poll interval rather than anything about
        # the workload. The signal that matters — a lease that lapsed — is
        # already captured on the claim that recovers it.
        return bool(self._inner.heartbeat(task_id, lease_seconds=lease_seconds))

    def record_abandoned(self, task_id: str, attempts: int) -> None:
        """Called by the dispatcher when a task exceeds max_attempts.

        Lives here rather than in the dispatcher so that everything emitting
        Hapax metrics does so through one object.
        """
        self._m.abandoned.add(1, telemetry.attrs(task_id=task_id, attempts=attempts))
