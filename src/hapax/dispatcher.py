"""The loop that picks up work nobody is doing any more.

Until this existed, recovery was *pull-based*: a task whose worker died stayed
`working` for ever unless something outside the system happened to retry it. In
practice that "something" is a queue redelivery or a human noticing, and the
guarantee quietly depended on it.

The dispatcher removes that dependency. It claims a task, runs the handler, and
repeats; a task whose claimant dies has its lease lapse and gets claimed by the
next sweep. Nothing has to detect the crash or report it — the absence of a
heartbeat *is* the detection.

    dispatcher = Dispatcher(store, handler=process)
    dispatcher.run_forever(poll_interval=1.0)

What this does not change is the exactly-once guarantee, and it is worth being
precise about why. The dispatcher only decides *who runs a task*; it never
performs the side effect itself. The effect and the terminal state still commit
in one transaction, and terminal tasks are never claimable, so re-dispatching a
task whose effect already landed is harmless: the handler reads the terminal
state and declines. Claiming twice is possible under a long enough pause; a
double side effect still is not.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .task import Task

log = logging.getLogger("hapax.dispatcher")


class _Store(Protocol):
    def claim_task(self, *, lease_seconds: float = 30) -> Task | None: ...
    def get_task(self, task_id: str) -> Task: ...


@dataclass
class DispatchStats:
    """What the loop has done, for tests and for anything watching it."""

    claimed: int = 0
    completed: int = 0
    failed: int = 0
    abandoned: int = 0
    sweeps: int = 0


@dataclass
class Dispatcher:
    """Claims tasks and runs them.

    `handler` is called with the claimed Task and is responsible for doing the
    work and moving the task to a terminal state — the same contract the worker
    already has, so a handler is usually a one-line adapter over it.

    `max_attempts` is the poison-task guard. A task that kills every worker that
    touches it would otherwise be claimed, crash its claimant, lapse, and be
    claimed again for ever, which is a very effective way to take down a fleet.
    Past the limit the dispatcher stops claiming it and reports it as abandoned,
    leaving the row in place to be looked at rather than silently dropped.
    """

    store: _Store
    handler: Callable[[Task], None]
    lease_seconds: float = 30
    max_attempts: int = 5
    stats: DispatchStats = field(default_factory=DispatchStats)

    def sweep(self, *, limit: int = 100) -> int:
        """Claim and run up to `limit` tasks. Returns how many were run."""
        self.stats.sweeps += 1
        done = 0
        for _ in range(limit):
            task = self.store.claim_task(lease_seconds=self.lease_seconds)
            if task is None:
                break

            self.stats.claimed += 1
            if task.attempts > self.max_attempts:
                self.stats.abandoned += 1
                log.warning(
                    "task %s abandoned after %d attempts — not re-dispatching",
                    task.id, task.attempts,
                )
                continue

            try:
                self.handler(task)
                self.stats.completed += 1
            except Exception:
                # A handler that raises has not necessarily failed the task —
                # it may have died before touching it at all. Leave the state
                # alone and let the lease lapse, which puts the task back in
                # the claimable pool rather than guessing at what happened.
                self.stats.failed += 1
                log.exception("handler raised for task %s; leaving it to lapse", task.id)
            done += 1
        return done

    def run_forever(self, *, poll_interval: float = 1.0, stop: Callable[[], bool] | None = None) -> None:
        """Sweep until `stop()` returns True (or for ever if it is not given).

        Sleeps only when a sweep found nothing, so a backlog drains at full
        speed instead of one task per poll interval.
        """
        while not (stop and stop()):
            if self.sweep() == 0:
                time.sleep(poll_interval)
