"""The long-running process a deployment actually runs.

Worth being precise about what gets deployed here, because it is easy to get
backwards. `hapax.server` speaks MCP over **stdio** — an agent host spawns it as
a child process and talks to it down a pipe. It is not a network service, there
is no port to expose, and running it as a detached container would have it read
EOF on stdin and exit immediately.

What is worth running continuously is the *dispatcher*: the loop that claims
tasks whose lease has lapsed and runs them. That is the piece that makes a task
survive the death of whatever created it, and it is the reason the deployment
needs a durable database rather than a process-local dictionary.

So the deployed daemon is: connect to RDS, sweep for claimable work, run it,
repeat. Insert a task from anywhere and this will pick it up and complete it
exactly once.

    HAPAX_DATABASE_URL=postgresql://... python deploy/dispatcher_main.py

Imports the library, changes nothing in it.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hapax.dispatcher import Dispatcher
from hapax.postgres import PostgresTaskStore
from hapax.task import Task
from hapax.worker import ensure_ledger, process_charge

log = logging.getLogger("hapax.deploy")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("HAPAX_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,  # so `docker logs` and journald both see it
    )

    conninfo = os.environ.get("HAPAX_DATABASE_URL")
    if not conninfo:
        log.error("HAPAX_DATABASE_URL is not set")
        return 2

    lease_seconds = float(os.environ.get("HAPAX_LEASE_SECONDS", "30"))
    poll_interval = float(os.environ.get("HAPAX_POLL_INTERVAL", "1.0"))

    store = PostgresTaskStore(conninfo)
    ensure_ledger(store)
    log.info("connected; dispatching with a %.0fs lease", lease_seconds)

    # SIGTERM is how both Docker and systemd ask for a shutdown. Answering it
    # means an in-flight task finishes its current sweep rather than being cut
    # off — and anything genuinely interrupted is recovered by its lease lapsing,
    # which is the whole design.
    stopping = threading.Event()

    def stop(signum, _frame):
        log.info("signal %s received; finishing the current sweep", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def handler(task: Task) -> None:
        log.info("running task %s (attempt %d)", task.id, task.attempts)
        outcome = process_charge(conninfo, task.id, lease_seconds=lease_seconds)
        log.info("task %s -> %s", task.id, outcome)

    dispatcher = Dispatcher(store=store, handler=handler, lease_seconds=lease_seconds)

    try:
        dispatcher.run_forever(poll_interval=poll_interval, stop=stopping.is_set)
    finally:
        log.info(
            "stopped after %d sweeps: %d claimed, %d completed, %d failed, %d abandoned",
            dispatcher.stats.sweeps,
            dispatcher.stats.claimed,
            dispatcher.stats.completed,
            dispatcher.stats.failed,
            dispatcher.stats.abandoned,
        )
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
