"""Shared fixtures.

The `store` fixture is parametrized over every backend, so the contract test
suite runs unchanged against both the in-memory reference and Postgres. The
Postgres runs are skipped unless HAPAX_TEST_DATABASE_URL points at a database —
that keeps `pytest` green on a machine with no database, while CI (and local
dev with the throwaway Postgres) exercises the real thing.
"""

import os

import pytest

from hapax.memory import InMemoryTaskStore

POSTGRES_URL = os.environ.get("HAPAX_TEST_DATABASE_URL")


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        yield InMemoryTaskStore()
        return

    if not POSTGRES_URL:
        pytest.skip("HAPAX_TEST_DATABASE_URL not set; skipping Postgres backend")

    from hapax.postgres import PostgresTaskStore

    s = PostgresTaskStore(POSTGRES_URL)
    # Clean slate for each test so runs are independent.
    with s._conn.cursor() as cur:
        cur.execute("TRUNCATE tasks")
    s._conn.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def pg_conninfo():
    """The Postgres URL, for tests that must hand it to a subprocess."""
    if not POSTGRES_URL:
        pytest.skip("HAPAX_TEST_DATABASE_URL not set; needs Postgres")
    return POSTGRES_URL


@pytest.fixture
def pg_store(pg_conninfo):
    """A durable store only — for tests where the in-memory backend has nothing
    to say, because the thing under test is surviving a process death."""
    from hapax.postgres import PostgresTaskStore
    from hapax.worker import ensure_ledger

    s = PostgresTaskStore(pg_conninfo)
    ensure_ledger(s)
    with s._conn.cursor() as cur:
        cur.execute("TRUNCATE tasks, ledger")
    s._conn.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def ledger(pg_store):
    """Reads the side-effect table — the only real evidence of exactly-once."""

    class Ledger:
        def rows(self, task_id: str) -> int:
            with pg_store._conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM ledger WHERE task_id = %s", [task_id])
                n = cur.fetchone()["n"]
            pg_store._conn.commit()
            return int(n)

        def total(self, task_id: str) -> int:
            with pg_store._conn.cursor() as cur:
                cur.execute(
                    "SELECT coalesce(sum(amount), 0) AS s FROM ledger WHERE task_id = %s",
                    [task_id],
                )
                s = cur.fetchone()["s"]
            pg_store._conn.commit()
            return int(s)

    return Ledger()
