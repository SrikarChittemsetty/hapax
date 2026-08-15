"""The Task model and its state machine.

This is the conceptual heart of the whole project. Everything else — the
in-memory store, the Postgres backend, the crash-recovery logic — is built to
protect the invariants defined here:

  1. A task is always in exactly one of five states.
  2. Only an explicitly-allowed set of transitions can happen.
  3. The three terminal states (`completed`, `failed`, `cancelled`) have zero
     outgoing transitions — once you're there, you stay there forever.

Invariant (3) is what makes crash recovery safe: if the store comes back up and
sees a task is already `completed`, it knows the work is done and must not run
it again.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .errors import InvalidTransition


class TaskState(str, Enum):
    """The five states a task can be in, from the MCP Tasks spec.

    Subclassing `str` means a `TaskState` *is* a string ("working" == the enum
    member), which makes it trivial to serialize to JSON or a database column
    without extra conversion code.
    """

    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# The three states a task can never leave. Kept as a frozenset because it's a
# fixed, hashable set we only ever read.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
)


# The transition table. For each state, the exact set of states it is allowed to
# move to. This is the design Srikar reasoned his way to: NOT a strict linear
# hierarchy (input_required can loop back to working once it gets its answer),
# but an explicit per-state allow-list. Terminal states map to the empty set.
ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.WORKING: frozenset(
        {
            TaskState.INPUT_REQUIRED,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.INPUT_REQUIRED: frozenset(
        {
            TaskState.WORKING,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def can_transition(from_state: TaskState, to_state: TaskState) -> bool:
    """Pure predicate: is moving from_state -> to_state legal?

    Kept separate from the Task object so it can be reasoned about and tested in
    isolation — the rule doesn't depend on any particular task's data.
    """
    return to_state in ALLOWED_TRANSITIONS[from_state]


def _now() -> datetime:
    """Timezone-aware UTC timestamp. Always store UTC; convert for display only."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Task:
    """A single long-running unit of work.

    Frozen (immutable): a transition doesn't mutate the task in place, it returns
    a *new* Task with the updated state. Immutability makes the state machine
    easier to reason about and rules out a whole class of "something changed this
    object behind my back" bugs — which matter a lot once concurrency and
    persistence enter the picture.
    """

    id: str
    state: TaskState
    # The original tool-call request. Hashed into the idempotency key by the
    # store; kept here so a recovered task knows what work it represents.
    input: dict[str, Any] = field(default_factory=dict)
    # Dedup key. Two create-requests with the same key refer to the same task,
    # so the underlying side effect happens at most once. None = not deduped.
    idempotency_key: str | None = None
    # Populated only in terminal states.
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    # Optional progress signal for `tasks/get` polling (0.0–1.0 + a message).
    progress: float | None = None
    progress_message: str | None = None
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    # When this task becomes eligible for TTL reaping. None = never expires.
    expires_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition_to(
        self,
        new_state: TaskState,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        progress: float | None = None,
        progress_message: str | None = None,
    ) -> Task:
        """Return a new Task in `new_state`, or raise InvalidTransition.

        This is the single choke point through which every state change must
        pass. Because it's the only way to change state, the transition table
        can't be bypassed — there's no setter that skips the check.
        """
        if not can_transition(self.state, new_state):
            raise InvalidTransition(self.state.value, new_state.value)

        return replace(
            self,
            state=new_state,
            result=result if result is not None else self.result,
            error=error if error is not None else self.error,
            progress=progress if progress is not None else self.progress,
            progress_message=(
                progress_message
                if progress_message is not None
                else self.progress_message
            ),
            updated_at=_now(),
        )
