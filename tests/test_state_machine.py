"""Tests for the Task state machine.

The point of these tests is to prove the two invariants that everything else
depends on: legal transitions succeed, and every illegal transition — most
importantly, any move *out of* a terminal state — is rejected.
"""

import pytest

from hapax.errors import InvalidTransition
from hapax.task import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    Task,
    TaskState,
    can_transition,
)


def make_task(state: TaskState = TaskState.WORKING) -> Task:
    return Task(id="t-1", state=state)


# --- legal transitions succeed -------------------------------------------------


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (TaskState.WORKING, TaskState.INPUT_REQUIRED),
        (TaskState.WORKING, TaskState.COMPLETED),
        (TaskState.WORKING, TaskState.FAILED),
        (TaskState.WORKING, TaskState.CANCELLED),
        (TaskState.INPUT_REQUIRED, TaskState.WORKING),
        (TaskState.INPUT_REQUIRED, TaskState.FAILED),
        (TaskState.INPUT_REQUIRED, TaskState.CANCELLED),
    ],
)
def test_legal_transitions_are_allowed(from_state, to_state):
    task = make_task(from_state)
    moved = task.transition_to(to_state)
    assert moved.state == to_state
    # The original object is untouched (immutability).
    assert task.state == from_state


def test_input_required_can_loop_back_to_working():
    """The transition that breaks a naive 'strict hierarchy' model: a task that
    asked for input resumes working once it gets an answer."""
    task = make_task(TaskState.INPUT_REQUIRED)
    assert task.transition_to(TaskState.WORKING).state == TaskState.WORKING


# --- terminal states are truly terminal ---------------------------------------


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES, key=lambda s: s.value))
@pytest.mark.parametrize("target", list(TaskState))
def test_no_transition_leaves_a_terminal_state(terminal, target):
    """From any terminal state, to any state at all, the move must be rejected.

    This is the single most important property in the project: it's what lets a
    recovered store trust that a `completed` task's side effect already happened
    and must not be re-run.
    """
    task = make_task(terminal)
    with pytest.raises(InvalidTransition):
        task.transition_to(target)


def test_terminal_states_have_no_outgoing_edges():
    for terminal in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()


# --- a representative illegal non-terminal transition -------------------------


def test_cannot_skip_from_input_required_to_completed():
    """input_required must go back through working before completing — it can't
    jump straight to completed. (A task waiting on input hasn't finished yet.)"""
    task = make_task(TaskState.INPUT_REQUIRED)
    with pytest.raises(InvalidTransition):
        task.transition_to(TaskState.COMPLETED)


# --- transition carries result/error payloads ---------------------------------


def test_completing_attaches_result_and_stamps_updated_at():
    task = make_task(TaskState.WORKING)
    done = task.transition_to(TaskState.COMPLETED, result={"rows": 42})
    assert done.state == TaskState.COMPLETED
    assert done.result == {"rows": 42}
    assert done.is_terminal
    assert done.updated_at >= task.updated_at


def test_failing_attaches_error():
    task = make_task(TaskState.WORKING)
    failed = task.transition_to(TaskState.FAILED, error={"code": "upstream_5xx"})
    assert failed.state == TaskState.FAILED
    assert failed.error == {"code": "upstream_5xx"}


# --- the pure predicate agrees with the table ---------------------------------


def test_can_transition_matches_table():
    for from_state, allowed in ALLOWED_TRANSITIONS.items():
        for to_state in TaskState:
            assert can_transition(from_state, to_state) == (to_state in allowed)
