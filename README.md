# Hapax

[![CI](https://github.com/SrikarChittemsetty/hapax/actions/workflows/ci.yml/badge.svg)](https://github.com/SrikarChittemsetty/hapax/actions/workflows/ci.yml)

***hapax*** *(Greek, "once") — from* hapax legomenon*, a word that occurs exactly once.*

**Kill the server mid-payment and the customer still gets charged exactly once. Never zero times. Never twice.**

When an AI agent kicks off a long-running job — charge this card, send this email, book this flight — and the machine running it dies halfway through, one of two bad things normally happens: the work is silently lost, or it is retried and *happens twice*. Hapax is the storage layer that makes neither possible.

The guarantee is not asserted. It is tested against a real Postgres database with real `kill -9`s, and measured against the approach most people write first.

|  | Hapax | The naive approach |
|---|---|---|
| 500 crashes at randomly-timed `kill -9`s | **0 double charges** | **121 double charges** |
| 16 simultaneous retries of the same payment | **0 duplicate charges** | **14.99 duplicate charges** (avg/trial) |
| Killed with `kill -9` before commit | **charged once** | lost or duplicated |
| Killed with `kill -9` after commit | **charged once** | charged twice |

And nothing has to notice the crash for that to hold: a dead worker's lease
lapses and its job is picked up automatically, median **20 ms** later, with no
retry from outside the system.

**[▶ Watch it happen — no install required](https://srikarchittemsetty.github.io/hapax/)** — a step-by-step replay of a real recorded crash run.

---

## The gap this fills

MCP's Tasks extension standardizes the *interface* for long-running tool calls (`tasks/get`, `tasks/update`, `tasks/cancel`) and a task lifecycle. But the reference SDKs ship only an **in-memory** store; persistence, idempotency, crash recovery, and TTL cleanup are explicitly left to whoever deploys it. This is a durable implementation of that store, plus the tests that prove the guarantee holds.

## The core guarantee

> A task's side effect happens **exactly once**, even if the process is hard-killed (`kill -9`) at any point — before the effect, mid-transaction, or after commit — and even if two workers race the same task simultaneously.

That claim isn't asserted; it's **tested against a real Postgres with real `SIGKILL`s** (see [Proof](#proof-not-claims)).

## See it work

Two ways, depending on how much of your time this deserves.

**In your browser, in ten seconds:** [**srikarchittemsetty.github.io/hapax**](https://srikarchittemsetty.github.io/hapax/) steps through a recorded run — the naive worker double-charging on the left, Hapax charging once on the right, with the command line, exit status and SQL result behind every claim. It is a recording of a real run produced by [`scripts/record_trace.py`](scripts/record_trace.py), not an animation; the raw evidence ships as [`docs/trace.json`](docs/trace.json).

**On your own machine, against your own Postgres:** `python scripts/demo.py` — real worker processes, real `SIGKILL`s, real rows in a database you can query afterwards.

![Crash-durability demo: a naive charge double-bills $100 after a crash, while this system charges exactly once ($50) through a crash before commit and a crash after commit.](demo.gif)

```
$ python scripts/demo.py

THE PROBLEM — a naive charge, crashed and retried
  charge sent, then killed (kill -9, rc=-9) 💥
  charge landed .................. ledger=$50, but the in-memory guard died with the process
  retry finds no record of the charge and sends it again
  ✗ customer charged $100 — DOUBLE CHARGED

WITH Hapax — CASE 1: crash BEFORE the charge commits
  task created .................... state=working
  worker killed mid-transaction (kill -9, rc=-9) 💥
  charge rolled back ............. ledger=$0, state=working
  recovery re-runs the worker .... state=completed
  ✓ customer charged exactly once: $50

WITH Hapax — CASE 2: crash AFTER the charge commits
  task created .................... state=working
  worker charged, then killed (kill -9, rc=-9) 💥
  charge already landed .......... ledger=$50, state=completed
  recovery sees terminal ......... worker says 'noop-terminal' (refuses to re-charge)
  ✓ customer charged exactly once: $50

RESULT
  naive approach:        $100  (double charged)
  Hapax:                 $50   (exactly once, through two different crashes)
```

Run it yourself: `python scripts/demo.py --conninfo "host=127.0.0.1 port=5432 user=postgres dbname=mdt"`. To render it as a GIF: `brew install vhs && vhs scripts/demo.tape`.

## How it works

```
 agent starts a slow tool call
            │
            ▼
   create_task(idempotency_key)  ──►  dedup at the DB (INSERT … ON CONFLICT):
            │                          same key can never create a second task
            ▼
   state persisted in Postgres  (survives a crash)
            │
        work runs …
            │
   ┌────────┴──────────────── kill -9 ────────────────────┐
   │                                                        │
 before commit:                                  after commit:
 side effect + state change are ONE transaction, task is COMPLETED (terminal),
 so Postgres rolls BOTH back → task still WORKING → recovery sees "done" and
 a retry runs it cleanly, once           refuses to re-run the effect
   │                                                        │
   └───────────────► exactly one side effect ◄─────────────┘
```

### The state machine

Five states, with an explicit per-state allow-list of legal transitions. The three terminal states have **zero** outgoing transitions — that immutability is the property recovery relies on.

| state | may transition to |
|-------|-------------------|
| `working` | `input_required`, `completed`, `failed`, `cancelled` |
| `input_required` | `working` (resumes after getting input), `failed`, `cancelled` |
| `completed` / `failed` / `cancelled` | — (terminal, permanent) |

## How a dead worker gets noticed

Everything above assumes *something* eventually retries the task. That assumption
was doing real work, and it was unearned: a task whose worker died stayed
`working` for ever unless a queue redelivered it or a human noticed.

Leases remove the assumption. A worker claims a task by writing a deadline into
it; while that deadline is in the future, no dispatcher will hand the task to
anyone else. If the worker finishes, the task goes terminal and stops being
claimable at all. If the worker dies, nothing renews the deadline, it passes, and
the task returns to the claimable pool on its own. **The absence of a heartbeat
is the detection** — there is no liveness table to keep in sync with reality, and
nothing to notify.

Two details are load-bearing:

- **Claiming is a single statement.** `UPDATE … WHERE id = (SELECT … FOR UPDATE
  SKIP LOCKED LIMIT 1)`. A SELECT to find an expired task followed by an UPDATE to
  claim it is the same check-then-act race that `create_task` avoids at the other
  end of the lifecycle — two dispatchers would both see the same lapsed task and
  both take it. `SKIP LOCKED` is what lets several dispatchers work the same
  table without queueing behind each other.
- **The dispatcher is allowed to be wrong.** It only decides *who runs a task*; it
  never performs the side effect. Since terminal tasks are never claimable and the
  effect commits with the state change, re-dispatching a task whose effect already
  landed is harmless — the handler reads the terminal state and declines. Claiming
  twice is possible; charging twice is not.

Measured with the same chaos harness, but with **nothing retrying anything** —
recovery happens only because a lease lapsed:

| 500 randomly-timed kills, recovered automatically | |
|---|---|
| charged exactly once | **500 / 500** |
| double charges | **0** |
| time from crash to recovery, p50 | **20 ms** |
| p95 / max | 279 ms / 302 ms |

The distribution is bimodal on purpose: a kill that lands before the worker takes
its lease leaves the task instantly claimable, and one that lands after it waits
out the remaining lease. So recovery latency is bounded by the lease length,
which is the knob — 250 ms here, and a real deployment would trade it against how
long a legitimately slow worker should be left alone.

```bash
python bench/chaos.py --conninfo "…" --trials 500 --recovery dispatcher
```

## Design decisions (the interview talking points)

- **Idempotency is enforced by the database, not application code.** A `UNIQUE` index on `idempotency_key` plus `INSERT … ON CONFLICT DO NOTHING` means even two simultaneous requests with the same key produce exactly one task. A check-then-insert in Python would race; the DB constraint can't.
- **Concurrent updates are serialized with row locks.** `update_task` does `SELECT … FOR UPDATE` inside a transaction, so two workers can't both read the old state and stomp each other. If the state-machine check rejects the transition, the transaction rolls back and nothing persists.
- **The side effect and the state change commit in one transaction** (`complete_with_effect`). That's what makes a *local* side effect (a ledger insert) truly exactly-once: a crash before commit rolls back both.
- **Honest boundary:** for an *external* side effect (a Stripe call that can't join the transaction), true exactly-once is impossible — you get at-least-once plus an idempotency key at the external boundary = *effectively*-once. The `idempotency_key` column is exactly that boundary. This distinction is stated plainly rather than glossed over.
- **One store interface, two backends.** `InMemoryTaskStore` (the reference/control) and `PostgresTaskStore` (durable) satisfy the same `TaskStore` protocol, and the **same contract test suite runs against both** — proving the durable backend didn't change behavior, only added durability.

## Proof, not claims

- **`tests/test_crash_recovery.py`** — spawns the worker as a real OS process, kills it with an actual `SIGKILL` at each commit boundary (`before_commit`, `after_commit`), and asserts the ledger holds exactly one row across the crash + recovery. Also covers a crash-loop (repeated recovery stays exactly-once).
- **`tests/test_concurrency.py`** — launches two workers on the same task at the same instant; the row lock serializes them and the charge still happens exactly once.
- **`bench/chaos.py`** — because those three crash points were *chosen*, this kills the worker at a uniformly random moment across its measured lifetime instead. 500 randomized kills: **0 double charges, 0 lost charges**. The identical harness pointed at the naive control group (`--strategy naive`) produces **121 double charges out of 500**.
- **`tests/test_server.py`** — drives the MCP server, then spawns it for real, `SIGKILL`s it mid-flight, and asks a **different** server process against the same database for the result. An in-memory store passes every other test in that file and fails this one.
- **`tests/test_store_contract.py`** — the behavioral spec, run against both backends.
- **`tests/test_state_machine.py`** — every legal transition, and every illegal move out of a terminal state.

## Benchmark

Durability has a measurable cost, reported with percentiles (averages hide tail latency, which is what a durable commit actually incurs). Full methodology and environment in [`bench/RESULTS.md`](bench/RESULTS.md).

| backend | op | p50 (ms) | p99 (ms) | throughput (ops/s) |
|---------|----|----------|----------|--------------------|
| memory | create | 0.003 | 0.009 | 256,657 |
| postgres | create | 0.122 | 0.621 | 6,950 |
| postgres | update | 0.164 | 0.750 | 5,163 |

The ~30–50× latency gap *is* the fsync-and-network tax for crash-survival — measured, not asserted.

**Under concurrent load** — 20,000 tasks drained by N worker processes against one Postgres, median of 3 runs. Exactly-once is re-checked at every level:

| workers | tasks/s | efficiency | claim p99 | commit p99 | exactly-once |
|---|---|---|---|---|---|
| 1 | 1,895 | 100% | 0.35 ms | 0.58 ms | ✓ |
| **4** | **4,949** | 65% | 0.61 ms | 0.93 ms | ✓ |
| 16 | 3,653 | 12% | 28.0 ms | 34.0 ms | ✓ |
| 32 | 2,512 | 4% | 121 ms | 130 ms | ✓ |

Peak is ~4,950 tasks/s at 4 workers; past that, throughput falls and the tail collapses. Writing this benchmark is what found the worst bug in the project — every claim was sequentially scanning and sorting the entire queue, because the index was on the filter column instead of the sort key. Fixing it took a claim from **1.808 ms to 0.113 ms** and peak throughput from 2,950 to 4,950 tasks/s. Full diagnosis, including what the ceiling turned out to be and the three candidates that were ruled out with evidence, is in [`bench/RESULTS.md`](bench/RESULTS.md).

**Comparative correctness** — this system vs. the naive alternative (application-level check-then-insert) under a retry storm of N simultaneous same-key requests. Duplicate charges scale as ≈ N−1 for the naive approach; this system is exactly-once at every level:

| concurrent requests | naive: avg duplicate charges/trial | ours |
|---------------------|------------------------------------|------|
| 2  | 1.00  | 0 |
| 8  | 7.00  | 0 |
| 16 | 14.99 | 0 |

That gap — 0 vs. N−1 double-charges — *is* the thesis, measured: enforcing idempotency in the database (`INSERT … ON CONFLICT` on a `UNIQUE` index) rather than application code is the difference between correct and broken under contention. Full methodology in [`bench/RESULTS.md`](bench/RESULTS.md).

## Run it

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,postgres]"

# state-machine + in-memory tests need no database:
.venv/bin/pytest tests/test_state_machine.py tests/test_reaper.py

# full suite incl. crash recovery needs Postgres:
export HAPAX_TEST_DATABASE_URL="host=127.0.0.1 port=5432 user=postgres dbname=mdt"
.venv/bin/pytest

# benchmark:
.venv/bin/python bench/benchmark.py --conninfo "$HAPAX_TEST_DATABASE_URL"
```

## Speaking the protocol

There is an actual server. `python -m hapax.server --conninfo "…"` speaks
JSON-RPC 2.0 over stdio — `initialize`, `tools/list`, `tools/call`, `tasks/get`,
`tasks/list`, `tasks/cancel` — with every task living in Postgres instead of a
dictionary that dies with the process:

```
→ {"method":"initialize"}
← {"serverInfo":{"name":"hapax"},"capabilities":{"experimental":{"tasks":{...}}}}

→ {"method":"tools/call","params":{"name":"charge","arguments":{"customer":"demo","amount":50},
                                   "task":{"idempotencyKey":"demo-1"}}}
← {"resultType":"task","taskId":"task_cbbf8ac…","status":"working"}

→ (the identical request again, as a retrying client would send it)
← {"resultType":"task","taskId":"task_cbbf8ac…","status":"working"}     ← same task
```

An ordinary `tools/call` blocks until the tool returns. A **task-augmented** one
returns a task id immediately and runs the work off the request path, so the
agent polls `tasks/get` for it — and because the task is in Postgres, *"the agent
polls later"* and *"the server was restarted in between"* are the same case.
[`tests/test_server.py`](tests/test_server.py) proves that literally: it spawns
the server, starts a task, `SIGKILL`s the process, spawns a **different** server
against the same database, and asks it for the result. An in-memory store passes
every other test in that file and fails this one.

Dispatch is separated from transport, so the whole protocol surface is tested
with plain dicts and only the two durability tests need a pipe.

`protocol.py` is the thin SEP-2663 wire adapter underneath it: it maps store operations to the
`tasks/*` result shapes an MCP server puts on the wire (`create` → task
envelope, `tasks/get` → DetailedTask with the result inlined on completion,
`tasks/cancel` → ack), with no transport coupling so it stays testable with plain
dicts. `tests/test_protocol.py` includes a **protocol-level durability test**: a
task created and completed through the adapter is still retrievable, with its
result, from a fresh store instance pointed at the same database (i.e. after a
restart).

Honest scope: this implements the SEP-2663 methods and wire *fields*, not the
whole of MCP — there is no capability negotiation beyond advertising the
extension, no progress notifications, and no input-required round trip. The official Python SDK does not yet implement Tasks
([python-sdk #2806](https://github.com/modelcontextprotocol/python-sdk/issues/2806) /
[#3005](https://github.com/modelcontextprotocol/python-sdk/pull/3005)); the intent
is to conform to that `TaskStore` interface once it lands.

## The full decision log

Every major choice — with the alternatives considered, why they were rejected,
and the failure mode each guards against — is written up in [`DESIGN.md`](DESIGN.md).
That's the document to read alongside the source (and the one to have in mind for
"why not X?" questions).

## Scope & honest limitations

- Single-node Postgres; no leader election or multi-region. The durability story is "survive process crash," not "survive datacenter loss."
- The benchmark is single-process, single-connection — it measures the store's own overhead, not a production deployment under concurrent load.
- Leases are wall-clock based. A worker that is merely paused — a long GC, a stopped container, a clock jump — can have its lease lapse while it is still alive, and the task gets handed to someone else. That is survivable here *because* the effect is exactly-once regardless of how many workers run it, which is rather the point; but it does mean the lease length is a real tuning decision, not a formality.

## Roadmap

- Temporal-wrapped baseline benchmark (answer "doesn't Temporal already do this?" with numbers).
- Multiple dispatcher processes sharing a fleet, with the lease length tuned per workload rather than fixed.

## Why it exists

A deep, hands-on build of the hard parts of durable execution — idempotency, exactly-once-ish semantics, crash recovery — applied to a real, currently-unsolved gap in a fast-moving protocol. Inspired by observability/reliability work on long-running background jobs.

## License

MIT — see [LICENSE](LICENSE).
