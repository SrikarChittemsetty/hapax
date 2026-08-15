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
