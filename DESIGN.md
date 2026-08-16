# Design decisions

This is the decision log the code comments only hint at: for each major choice,
what was chosen, the alternatives considered, why they were rejected, and the
failure mode the choice guards against. It's meant to be read alongside the
source — and to be the thing you can defend when someone asks "why not X?"

---

## 1. State transitions as an explicit allow-list table

**Chosen:** a `dict[TaskState, frozenset[TaskState]]` mapping each state to the
exact set of states it may become. The only way to change state is
`Task.transition_to`, which consults the table.

**Alternatives rejected:**
- *Enum ordering / integer ranks* (`completed > working`, allow only forward
  moves). Rejected because the lifecycle isn't linear: `input_required` legally
  goes **back** to `working`. A total order can't express a legal backward edge.
- *Scattered `if`/`else` checks at each call site.* Rejected because the rules
  would live in many places and drift; there'd be no single source of truth and
  no way to prove completeness.
- *A general state-machine library.* Rejected as overkill — five states and a
  handful of edges don't justify a dependency, and the table is more auditable.

**Failure mode it guards against:** an illegal transition (e.g. reopening a
`completed` task) silently corrupting state. With the table, the illegal move
raises `InvalidTransition` before anything is written.

---

## 2. Terminal states have an *empty* transition set

**Chosen:** `completed`, `failed`, `cancelled` map to `frozenset()` — zero
outgoing edges — and `frozenset` makes that immutable at runtime.

**Alternatives rejected:**
- *Enforce terminality only in recovery code.* Rejected: the guarantee would be
  one forgotten check away from breaking. Encoding it in the table means it's
  enforced everywhere `transition_to` is used, for free.

**Failure mode it guards against:** the whole crash-recovery guarantee. Recovery
trusts that a `completed` task's side effect already happened and refuses to
re-run it. That trust is only safe if `completed` can *never* flip back — so
terminality must be a hard invariant, not a convention.

---

## 3. `Task` is immutable (frozen dataclass)

**Chosen:** transitions return a *new* `Task` via `dataclasses.replace`, never
mutate in place.

**Alternatives rejected:**
- *Mutable dataclass with setters.* Rejected because shared mutable state is the
  source of "something changed this object behind my back" bugs, which get much
  worse once concurrency and caching enter. Immutability makes a `Task` value a
  safe thing to pass around and compare.

**Failure mode it guards against:** aliasing bugs — two references to the same
task disagreeing about its state after one of them mutates it.

---

## 4. One `TaskStore` Protocol, two backends

**Chosen:** a `typing.Protocol` defining the contract, with `InMemoryTaskStore`
(reference/control) and `PostgresTaskStore` (durable) both conforming
structurally. The **same contract test suite runs against both.**

**Alternatives rejected:**
- *A single Postgres-only class.* Rejected: no fast control to benchmark
  against, no cheap backend for tests that don't need durability, and no proof
  that "durable" didn't accidentally change behavior.
- *An abstract base class with inheritance.* Rejected in favor of a Protocol so a
  backend doesn't need to import or subclass anything — looser coupling.

**Failure mode it guards against:** the durable backend silently diverging from
the intended semantics. Behavioral parity is *tested*, not assumed.

---

## 5. Idempotency enforced by the database (`INSERT ... ON CONFLICT`)

**Chosen:** a `UNIQUE` index on `idempotency_key`, and create does
`INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING ...`; if no row
comes back, it `SELECT`s and returns the existing task.

**Alternatives rejected:**
- *Application-level check-then-insert* (SELECT to see if the key exists, then
  INSERT). Rejected because of the time-of-check-to-time-of-use race: two
  concurrent requests both see "not found" and both insert.
  **This isn't hand-waving — it's measured** in `bench/correctness.py`: under
  simultaneous same-key requests, the naive approach double-charges in 100% of
  trials, with duplicate charges scaling as ~N-1. The DB approach: zero.
- *A mutex / advisory lock around the check.* Rejected: serializes all creates
  through one lock (throughput cliff), and doesn't survive a crash mid-hold as
  cleanly as a constraint.
- *`INSERT` and catch the unique-violation `IntegrityError`.* Reasonable, and
  nearly equivalent — `ON CONFLICT` was chosen because it's a single round trip
  and expresses intent directly rather than via exception control-flow.

**Failure mode it guards against:** duplicate side effects (double-charge) from
retried or concurrent requests carrying the same idempotency key.

**Honest note:** NULL keys are allowed to repeat — Postgres treats NULLs as
distinct in a unique index — which is exactly right: a task with no key opts out
of dedup.

---

## 6. Concurrent updates serialized with `SELECT ... FOR UPDATE`

**Chosen:** `update_task` and `complete_with_effect` lock the row
(`SELECT ... FOR UPDATE`) inside a transaction, then read-modify-write.

**Alternatives rejected:**
- *Optimistic concurrency (a `version` column, compare-and-swap, retry on
  conflict).* A legitimate alternative with better throughput under low
  contention. Rejected here for *clarity*: pessimistic locking makes the
  read-modify-write obviously correct with no retry loop to reason about. Noted
  as a possible optimization.
- *No locking.* Rejected: two updaters could both read the old state and issue
  conflicting writes (lost update).

**Failure mode it guards against:** the lost-update / double-apply race — two
workers both moving a `working` task forward and both running the side effect.
Proven prevented in `tests/test_concurrency.py`.

---

## 7. Side effect + state change in ONE transaction (`complete_with_effect`)

**Chosen:** the caller's side effect (e.g. a ledger `INSERT`) and the transition
to `COMPLETED` commit together, in the same transaction.

**Alternatives rejected:**
- *Do the effect, then separately mark the task done.* Rejected: a crash between
  the two leaves the effect applied but the task not `completed` — recovery
  re-runs the effect → double side effect.
- *Transactional outbox / two-phase patterns.* The right tool when the effect is
  in a *different* system; unnecessary complexity when the effect lives in the
  same database.

**Failure mode it guards against:** the classic "crashed between the write and
the bookkeeping" double-execution. One transaction means a crash before commit
rolls back *both*.

**Honest boundary — this is the most important nuance in the project:** true
exactly-once only holds when the side effect can join this transaction (same
Postgres). For an **external** effect (a Stripe charge over HTTP), it cannot —
you fundamentally get *at-least-once delivery plus an idempotency key at the
external boundary* = *effectively*-once. Claiming true exactly-once for an
external call would be wrong. The `idempotency_key` column is precisely that
external-boundary dedup handle.

---

## 8. Recovery is lease-based, and absence of a heartbeat is the signal

**Chosen:** a worker claims a task by writing a lease deadline into the row. If
it finishes, the task goes terminal and stops being claimable. If it dies,
nothing renews the deadline, the deadline passes, and the task returns to the
claimable pool for a dispatcher to pick up.

*(This section previously described recovery as deliberately pull-based — a
worker re-running a task and observing terminal state — with a push scheduler
listed as deferred roadmap. That was true until leases landed, and the honest
reason for the change is that the old design's gap was real: a task whose worker
died stayed `working` for ever unless something outside the system happened to
retry it, which meant the guarantee quietly depended on a queue redelivery or a
human noticing.)*

**Alternatives rejected:**
- *A liveness table — workers register, heartbeat, get marked dead.* Rejected:
  it is a second source of truth about who is alive, and it can disagree with
  reality in both directions. A lease on the task row cannot: the fact that
  matters (is anyone working on this?) is stored on the thing it is a fact about.
- *`SELECT` an expired task, then `UPDATE` to claim it.* Rejected for the same
  reason `create_task` does not check-then-insert: two dispatchers both see the
  same lapsed task and both claim it. Claiming is one statement —
  `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`.
- *Plain `FOR UPDATE` without `SKIP LOCKED`.* Rejected: a second dispatcher would
  block on the row the first is claiming instead of moving on to the next task,
  turning a fleet into a queue.

**Failure mode it guards against:** work stranded for ever because the only
process that knew about it died. Measured: 500 randomized kills recovered with
nothing retrying, 0 double charges, p50 20 ms crash-to-recovery.

**Its limit, stated plainly:** leases are wall-clock. A worker that is merely
paused — a long GC, a stopped container, a clock jump — can lose its lease while
still alive, and its task will be handed to someone else. That is survivable here
*because* the effect is exactly-once no matter how many workers run it, which is
rather the point; but it makes the lease length a real tuning decision.

---

## 8a. The claim index is on the sort key, not the filter column

**Chosen:** `CREATE INDEX idx_tasks_claim_order ON tasks (created_at) WHERE
state = 'working'`, with the lease check applied as a filter while scanning.

**What was there first, and why it was wrong:** the obvious index is on
`lease_expires_at` — it is the column in the `WHERE` clause. But the claim also
says `ORDER BY created_at`, and an index on the filter column cannot serve that
ordering, so Postgres sequentially scanned every claimable row and quicksorted
all of them to pick one. The cost of a claim therefore grew with the depth of the
queue, which the load benchmark caught as single-worker throughput falling from
1,714 to 560 tasks/s purely as the queue got longer.

Indexing the sort key instead lets the claim walk rows in `created_at` order and
stop at the first one it can take: **1.808 ms → 0.113 ms per claim**, peak
throughput 2,950 → 4,950 tasks/s.

**A knock-on that had to change with it:** the predicate was
`lease_expires_at IS NULL OR lease_expires_at < now()`. The `OR` form stopped the
planner treating it as a simple filter, so it became
`coalesce(lease_expires_at, '-infinity') < now()`.

**Its limit:** this is the right trade while most queued tasks are unleased,
which is the normal state. If nearly everything were leased at once, the scan
would walk further before finding a free row.

---

## 8b. Schema migration checks before it reaches for DDL

**Chosen:** `apply_schema` queries `information_schema` first and only runs DDL
when the schema is genuinely behind, with a Postgres advisory lock serializing
the processes that do need to migrate.

**Why it is not just `CREATE ... IF NOT EXISTS` everywhere:** every store applies
the schema on connect, so N workers starting together all run the same DDL. That
is harmless for `CREATE ... IF NOT EXISTS`, and *not* harmless for
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, which takes an AccessExclusiveLock on
the table before it can decide there is nothing to do. Two workers doing that
simultaneously deadlock each other — which is exactly how this was found, by the
two-workers-racing test starting to fail the moment the lease columns were added.

**Failure mode it guards against:** a deadlock on startup, and an exclusive table
lock taken on the hot path by every process that connects.

---

## 9. TTL reaper as a separate sweep

**Chosen:** a background loop deletes terminal tasks whose `expires_at` passed;
a partial index `(state, expires_at) WHERE expires_at IS NOT NULL` supports it.

**Alternatives rejected:**
- *Filter expired tasks out at query time and never delete.* Rejected: the table
  grows without bound; every query pays for dead rows forever.
- *Rely on a DB-native TTL / partition-drop.* Postgres has no row TTL; partition
  rotation is heavier machinery than a small sweep needs at this scale.

**Failure mode it guards against:** unbounded table growth. Only *terminal* tasks
are reaped — in-flight work is never deleted out from under a caller.

---

## 10. Timestamps are `timestamptz`, stored UTC

**Chosen:** every timestamp is timezone-aware UTC (`timestamptz` column,
`datetime.now(timezone.utc)`).

**Alternative rejected:** naive local-time datetimes. Rejected because they're
ambiguous across DST and across machines in different zones — a classic source of
"the reaper deleted things an hour early/late" bugs.

---

## 11. Sync API (not async)

**Chosen:** synchronous methods, for readability and easy reasoning about the
transactional read-modify-write.

**Alternative / honest tradeoff:** real MCP servers are async (asyncio). A
production binding would want an async store (`psycopg`'s async mode). Chosen sync
here to keep the concurrency story about *database* locking rather than about
event-loop mechanics — the crash/concurrency proofs use real OS processes, which
sidesteps the sync/async question entirely. Async is a mechanical port, not a
redesign.

---

## 12. Our state model vs. SEP-2663's final semantics

**Honest divergence worth knowing:** SEP-2663's final draft reserves `failed`
for *JSON-RPC transport* errors and treats a tool result with `isError: true` as
a **completed** task. This project models `failed` as a first-class task state
for *any* failure. When binding to the official wire protocol (see `protocol.py`
and the roadmap), a tool-level error maps to `completed` with an error result,
per the spec — the store's `failed` state then represents infrastructure/JSON-RPC
failures. This is called out so the mapping is deliberate, not accidental.

---

## 13. The MCP server separates dispatch from transport

**Chosen:** `HapaxServer.handle(request_dict) -> response_dict` holds the entire
protocol surface; `serve_stdio()` is a thin loop that reads lines, calls it, and
writes lines.

**Alternatives rejected:**
- *Handle framing and dispatch together in the read loop.* Rejected: every
  protocol test would then need a pipe, a subprocess and a timeout, which makes
  the tests slow, flaky and bad at saying what broke. As split, thirteen tests
  drive plain dicts and only the two that genuinely need a process — the ones
  that kill the server — pay for one.
- *Run task work on the request thread.* Rejected: that is what a task-augmented
  call exists to avoid. The point of returning a task id is that the caller does
  not wait, so the work runs off the request path and the client polls
  `tasks/get`.

**Failure mode it guards against:** the one the project exists for, now visible
at the protocol level. Because the task lives in Postgres, *"the agent polls
later"* and *"the server was restarted in between"* are the same case —
`tests/test_server.py` spawns the server, starts a task, `SIGKILL`s it, spawns a
different server against the same database and asks that one for the result. An
in-memory store passes every other test in that file and fails this one.

A retried `tools/call` carrying the same idempotency key returns the same
`taskId` and does not re-run the tool, so the guarantee is observable from
outside the process rather than only in the store's own tests.

**Honest scope:** the methods and wire fields, not the whole of MCP. No
capability negotiation beyond advertising the extension, no progress
notifications, no input-required round trip.
