"""TTL reaper — the background janitor that deletes expired, finished tasks.

Without this, the tasks table grows forever: every completed/failed/cancelled
task lingers long after anyone cares about its result. The reaper periodically
sweeps and deletes terminal tasks whose `expires_at` has passed. In-flight work
is never touched — `reap_expired` only removes tasks in a terminal state.

The loop is written to be *testable*: the sleep and the stop condition are
injectable, so a test can run a bounded number of sweeps with no real waiting,
instead of the loop being an untestable `while True: sleep`.
"""

from __future__ import annotations

import argparse
import time
from typing import Callable

from .store import TaskStore


def reap_once(store: TaskStore) -> int:
    """Run a single sweep. Returns how many tasks were reaped."""
    return store.reap_expired()


def run_reaper(
    store: TaskStore,
    *,
    interval_seconds: float = 60.0,
    max_sweeps: int | None = None,
    should_stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_sweep: Callable[[int], None] | None = None,
) -> int:
    """Sweep every `interval_seconds` until stopped. Returns total reaped.

    Stops when either `max_sweeps` sweeps have run or `should_stop()` returns
    True. With neither set it runs forever (the production daemon case). `sleep`
    and `should_stop` are injectable purely so tests can drive the loop
    deterministically without wall-clock waiting.
    """
    total = 0
    sweeps = 0
    while True:
        if should_stop is not None and should_stop():
            break
        if max_sweeps is not None and sweeps >= max_sweeps:
            break

        removed = reap_once(store)
        total += removed
        sweeps += 1
        if on_sweep is not None:
            on_sweep(removed)

        # Don't sleep after the final sweep — avoids a pointless trailing wait.
        if max_sweeps is not None and sweeps >= max_sweeps:
            break
        sleep(interval_seconds)

    return total


def main() -> None:
    ap = argparse.ArgumentParser(description="TTL reaper daemon for the task store.")
    ap.add_argument("--conninfo", required=True, help="Postgres connection string")
    ap.add_argument(
        "--interval", type=float, default=60.0, help="seconds between sweeps"
    )
    args = ap.parse_args()

    # Imported here so the module stays importable without psycopg installed.
    from .postgres import PostgresTaskStore

    store = PostgresTaskStore(args.conninfo)

    def report(removed: int) -> None:
        if removed:
            print(f"reaped {removed} expired task(s)", flush=True)

    run_reaper(store, interval_seconds=args.interval, on_sweep=report)


if __name__ == "__main__":
    main()
