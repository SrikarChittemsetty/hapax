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
