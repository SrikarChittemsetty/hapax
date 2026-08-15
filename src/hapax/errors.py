"""Error taxonomy for the task store.

These are *typed* errors, not bare strings, so callers (and tests) can react to
a specific failure mode instead of pattern-matching on a message. This is the
same idea as the structured-error contract in the MCP spec: a machine-actionable
code, not just human-readable prose.
"""


class TaskError(Exception):
    """Base class for every error this library raises."""


class InvalidTransition(TaskError):
    """Raised when code tries to move a task into a state the state machine
    forbids — e.g. reopening a `completed` task, or cancelling one that already
    `failed`. This is the guard that makes terminal states actually terminal.
    """

    def __init__(self, from_state: str, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"illegal transition: {from_state} -> {to_state}")


class TaskNotFound(TaskError):
    """Raised when a task id (or idempotency key) doesn't exist in the store."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"task not found: {task_id}")
