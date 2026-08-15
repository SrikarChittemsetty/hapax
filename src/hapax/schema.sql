-- Schema for the durable task store.
--
-- Design decisions worth defending:
--   * idempotency_key is UNIQUE. This is what enforces dedup at the DATABASE
--     level, not in application code. Two concurrent create requests with the
--     same key can race in the app, but the unique index guarantees only one
--     row can ever exist — the loser is handled by INSERT ... ON CONFLICT.
--     NULL keys are allowed to repeat (a task with no key never dedups), which
--     is exactly the semantics we want: Postgres treats NULLs as distinct in a
--     unique index.
--   * All timestamps are timestamptz (stored UTC). Never store naive local time.
--   * input/result/error are jsonb so the store stays payload-agnostic — it
--     doesn't care what the tool actually does.

CREATE TABLE IF NOT EXISTS tasks (
    id               text        PRIMARY KEY,
    state            text        NOT NULL,
    input            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key  text        UNIQUE,
    result           jsonb,
    error            jsonb,
    progress         double precision,
    progress_message text,
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,
    expires_at       timestamptz
);

-- Supports the TTL reaper's query: "terminal tasks whose expiry has passed".
-- Without this the reaper would scan the whole table every sweep.
CREATE INDEX IF NOT EXISTS idx_tasks_reap
    ON tasks (state, expires_at)
    WHERE expires_at IS NOT NULL;

-- Leases: how the system notices a worker died instead of waiting to be told.
--
-- A worker claims a task by writing a lease deadline into it. If the worker
-- finishes, the task goes terminal and stops being claimable. If the worker
-- dies, nothing writes anything — the deadline simply passes, and the task
-- becomes claimable again. Absence of a heartbeat is the signal; no separate
-- liveness tracking, and nothing to get out of sync with reality.
--
-- attempts is kept for two reasons: it makes "this task has been picked up 40
-- times" visible instead of silent, and it gives a dispatcher the number it
-- needs to stop retrying a task that kills every worker that touches it.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;

-- Supports the claim query: "an unleased or lease-expired task still working".
-- Partial on state so the index only carries rows that could ever be claimed.
CREATE INDEX IF NOT EXISTS idx_tasks_claimable
    ON tasks (lease_expires_at)
    WHERE state = 'working';
