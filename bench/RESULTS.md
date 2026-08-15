# Benchmark results

**What this measures:** per-operation latency (percentiles) and throughput for
the core store operations, comparing the in-memory reference backend against the
durable Postgres backend. The gap between them is the cost of durability — the
price paid, per operation, to survive a crash.

**How to reproduce:**

```bash
python bench/benchmark.py --conninfo "host=127.0.0.1 port=5432 user=postgres dbname=mdt" \
    --iterations 2000 --warmup 200
```

## Environment

| | |
|---|---|
| Machine | Apple Silicon (arm64), macOS |
| Python | 3.12 |
| Postgres | 16 (local, single connection, TCP to 127.0.0.1) |
| Iterations | 2000 measured, 200 warmup (discarded) |
| Clock | `time.perf_counter` (monotonic, high-resolution) |

Single process, single connection. This is the store's own overhead, not a
distributed deployment.

## Results

| backend | op | n | mean (ms) | p50 | p95 | p99 | max | throughput (ops/s) |
|---------|----|---|-----------|-----|-----|-----|-----|--------------------|
| memory | create | 2000 | 0.004 | 0.003 | 0.004 | 0.009 | 1.676 | 256,657 |
| memory | get | 2000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 15,500,869 |
| memory | update | 2000 | 0.003 | 0.002 | 0.003 | 0.006 | 0.412 | 392,960 |
| postgres | create | 2000 | 0.144 | 0.122 | 0.263 | 0.621 | 1.379 | 6,950 |
| postgres | get | 2000 | 0.107 | 0.100 | 0.138 | 0.268 | 0.755 | 9,328 |
| postgres | update | 2000 | 0.194 | 0.164 | 0.326 | 0.750 | 2.178 | 5,163 |

## Reading the numbers

- **Durability costs ~30–50× in latency and ~1–2 orders of magnitude in
  throughput.** In-memory `create` runs at ~257k ops/s; durable `create` at
  ~7k ops/s. That is the fsync-and-network tax for state that survives a crash.
- **`update` is the most expensive durable op** (~0.16ms p50, ~0.75ms p99),
  which is expected: it's a `SELECT ... FOR UPDATE` (row lock) followed by an
  `UPDATE`, two statements plus a commit, versus `create`'s single insert.
- **Tail vs. median:** durable p99 is ~4–5× the p50. The tail is dominated by
  commit-time I/O — the reason percentiles, not averages, are the honest way to
  report a durable system's latency.

## Comparative correctness under concurrency

The latency numbers above measure *this system against itself* (durable vs.
in-memory floor). This section measures it against the **alternative most people
reach for first**: application-level "check if the key exists, then insert"
dedup, with no database constraint.

Both strategies face the identical workload — N simultaneous requests carrying
the same idempotency key (a retry storm / at-least-once delivery). The only
variable is the dedup mechanism. Correct behaviour = exactly one row created.

**Reproduce:**

```bash
python bench/correctness.py --conninfo "host=127.0.0.1 port=5432 user=postgres dbname=mdt" \
    --trials 200 --work-ms 2 --concurrency-levels "2,4,8,16"
```

**Result** (200 trials per level, requests released simultaneously — the retry-storm worst case):

| concurrent requests | naive: trials double-charged | naive: avg duplicate charges/trial | ours: double-charges |
|---------------------|------------------------------|------------------------------------|----------------------|
| 2  | 200/200 (100%) | 1.00  | 0/200 |
| 4  | 200/200 (100%) | 3.00  | 0/200 |
| 8  | 200/200 (100%) | 7.00  | 0/200 |
| 16 | 200/200 (100%) | 14.99 | 0/200 |

**Reading it:** the naive approach's duplicate charges scale as ≈ **N − 1** — under
simultaneous arrival, all N requests pass the existence check before any of them
inserts, so every one creates a charge. `INSERT ... ON CONFLICT` against a UNIQUE
index is **exactly-once at every concurrency level**. This is the whole thesis,
measured: pushing the guarantee into the database is not a stylistic choice, it's
the difference between 0 and N−1 double-charges under contention.

(Simultaneous release is the worst case; it's also a realistic one — retry storms
and at-least-once queues deliver duplicates in tight bursts. The point is that the
naive approach has no safe floor under contention, while the DB-level approach has
no failures at all.)

## Randomized crash testing (chaos)

The crash-recovery tests kill the worker at three *chosen* points — before the
effect, before the commit, after the commit. That is the right place to start and
the wrong place to stop: hand-picked crash points only prove the guarantee holds
where the author thought to look. `bench/chaos.py` removes the choosing.

**Method.** Each trial spawns a real worker, waits a uniformly random interval,
and hard-kills it with `SIGKILL` from the parent. The interval is drawn from
`[0, 1.15 × L)` where `L` is the worker's *measured* median lifetime — calibrated
per run, because spreading kills over the job duration alone would land every one
of them in Python interpreter startup and prove nothing. Recovery then runs, and
the database is checked for the invariant: exactly one charge, totalling $50.
The same harness runs the naive control group (`--strategy naive`) against the
identical distribution of kill times.

```bash
python bench/chaos.py --conninfo "…" --trials 500              # Hapax
python bench/chaos.py --conninfo "…" --trials 500 --strategy naive
```

**Result** (500 trials each, seed 1234, worker lifetime ≈ 200 ms):

| | Hapax | Naive (in-memory dedup) |
|---|---|---|
| charged exactly once | **500 / 500** | 379 / 500 |
| double charges | **0** | **121 (24.2%)** |
| lost charges | 0 | 0 |

Where the kills actually landed, Hapax run: 302 before the charge committed, 29
after it committed, 169 after the worker had already exited. The naive run drew
from the same distribution (380 / 32 / 88).

**Reading it:** roughly a quarter of randomly-timed crashes are enough to make the
naive worker bill a customer twice, and none of them are enough to make Hapax do
it. The failures are not exotic — they are the ordinary case of a process dying
after its side effect committed but before anything durable recorded that fact.

## Automatic recovery (no retry from outside)

The chaos numbers above still had a retry doing the recovering. This run removes
it: the worker takes a lease, gets killed, and **nothing retries anything**. The
task comes back only because the lease lapsed and a dispatcher swept it up.

```bash
python bench/chaos.py --conninfo "…" --trials 500 --recovery dispatcher
```

**Result** (500 trials, seed 1234, 250 ms lease):

| | |
|---|---|
| charged exactly once | **500 / 500** |
| double charges | **0** |
| lost charges | **0** |
| crash → recovery, p50 | **20 ms** |
| p95 | 279 ms |
| max | 302 ms |

**Reading it:** the distribution is bimodal by construction. A kill that lands
before the worker has taken its lease leaves the task immediately claimable, so
recovery is a single sweep away (~20 ms). A kill that lands after it has to wait
out the remainder of the lease, which puts the tail just above the 250 ms lease
length. Recovery latency is therefore bounded by the lease, not by anything in
the store — the tuning question is how long a genuinely slow worker deserves
before it is presumed dead, and the answer is a deployment decision rather than
a property of this code.

Note also what the kill distribution says: 32 of the 500 kills landed *after* the
charge committed. Every one of those was re-dispatched, and every one of those
re-dispatched workers declined to charge again.

## Concurrency: how many workers can this actually feed?

Everything above measures correctness under adversity, and the latency table at
the top measures one process on one connection — the store's floor, not what
happens when sixteen workers fight over the same table. `bench/load.py` seeds N
tasks, starts W worker *processes* (processes, not threads, so the GIL is not
quietly serialising the thing being measured), and times the drain.

```bash
python bench/load.py --conninfo "…" --tasks 20000 --workers 1,2,4,8,16,32 --repeat 3
```

**Result** (20,000 tasks per level, median of 3 runs — a single run of this
varies by a third depending on what else the laptop is doing):

| workers | tasks/s | speedup | efficiency | claim p50 | claim p99 | commit p50 | commit p99 | exactly-once |
|---|---|---|---|---|---|---|---|---|
| 1  | 1,895 | 1.00× | 100% | 0.18 ms | 0.35 ms | 0.29 ms | 0.58 ms | ✓ |
| 2  | 3,518 | 1.86× | 93% | 0.19 ms | 0.40 ms | 0.31 ms | 0.69 ms | ✓ |
| **4** | **4,949** | **2.61×** | 65% | 0.28 ms | 0.61 ms | 0.46 ms | 0.93 ms | ✓ |
| 8  | 3,711 | 1.96× | 24% | 0.56 ms | 6.68 ms | 0.92 ms | 12.6 ms | ✓ |
| 16 | 3,653 | 1.93× | 12% | 0.84 ms | 28.0 ms | 1.14 ms | 34.0 ms | ✓ |
| 32 | 2,512 | 1.33× | 4% | 1.57 ms | 121 ms | 2.04 ms | 130 ms | ✓ |

Peak is **~4,950 tasks/s at 4 workers**. Past that, throughput falls and the tail
degrades hard — p99 goes from sub-millisecond to 130 ms between 4 workers and 32.
The exactly-once invariant is re-checked at every level and in every repeat,
because a throughput number from a run that double-charged somebody is worth
less than no number at all.

### The bug this found

The first version of this benchmark reported 560 tasks/s for a single worker,
against 1,714 tasks/s for the same worker on a 500-task queue. Throughput falling
as the queue *grows* is not a load characteristic, it is a bug, and `EXPLAIN
ANALYZE` named it immediately:

```
->  Sort  (cost=389.00..414.00 rows=10000)  (actual time=1.654..1.654)
      Sort Key: tasks_1.created_at
      Sort Method: quicksort  Memory: 1166kB
      ->  Seq Scan on tasks  (rows=10000)
```

Every claim was sequentially scanning the queue and quicksorting all 10,000
candidates to pick one task. The partial index was on `lease_expires_at`, which
is the column in the *filter*, while the cost was in the `ORDER BY created_at`.
Indexing the sort key instead lets the claim walk rows in order and stop at the
first one it can take:

```
->  Index Scan using idx_tasks_claim_order on tasks  (actual time=0.006..0.006)
      Filter: (state = 'working' AND COALESCE(lease_expires_at, '-infinity') < now())
```

**1.808 ms → 0.113 ms per claim (16×)**, single-worker throughput 560 → 1,895
tasks/s, and peak throughput 2,950 → 4,950. The `IS NULL OR …` in the original
predicate had to become a `coalesce` in the same change, because the OR form
stopped the planner using the index as a plain filter.

### What the ceiling actually is

Four candidate explanations, and only one survives contact with the evidence.

| candidate | evidence | verdict |
|---|---|---|
| The claim query | fixed above; `EXPLAIN` shows an index scan stopping at the first row | ruled out |
| CPU exhaustion | `iostat` during the 8-worker run: 11–13% user, 16–25% system, **63–73% idle** | not globally CPU-starved |
| Row-lock contention | `pg_stat_activity` sampled every 5 ms across a 16-worker run: `Lock/transactionid` in 343 of ~15,000 waits (2%) | minor — `SKIP LOCKED` is doing its job |
| Commit path | `LWLock/WALWrite` 1,767 + `IO/WALWrite` 378; `Client/ClientRead` 5,818 dominates overall | **this is the wall** |

The `ClientRead` figure is the interesting one: the single largest thing Postgres
does during a saturated run is *wait for the client*. Each task costs two
synchronous round-trips (claim, then commit-with-effect), so a worker's rate is
bounded by round-trip latency rather than by server capacity — which is why
adding workers helps at first and why the server-side commit path becomes the
shared limit once enough of them are in flight.

Turning `synchronous_commit` off isolates the fsync component:

| workers | fsync on | fsync off | delta |
|---|---|---|---|
| 1 | 1,923 | 2,358 | +23% |
| 4 | 5,634 | 7,483 | +33% |
| 8 | 5,622 | 6,181 | +10% |
| 16 | 4,564 | 4,684 | +3% |

So durability costs roughly a quarter to a third of throughput at low
concurrency — the honest price of the guarantee — but the *shape* of the curve is
unchanged, peak still lands at 4 workers, and the collapse past it still happens.
fsync is a real cost and not the ceiling.

**Honest scope.** Client and server share one 8 GB laptop with four performance
cores, so eight worker processes plus their eight Postgres backends are already
competing before any of this is a design question; the idle-CPU figure counts
efficiency cores that are not equivalent. These numbers are this machine's
ceiling, not a server's, and the useful part is the *shape* — near-linear to 4,
flat to 16, tail collapse at 32 — plus the fact that correctness never wavered
at any point on the curve.

## Roadmap

The intended next comparison is a **Temporal-wrapped baseline** running the same
task lifecycle, to answer "doesn't Temporal already do this?" with a measured
latency/throughput delta rather than an assertion. Note the honest framing: the
goal there is *comparable latency at a fraction of the operational footprint* for
this specific workload — not "faster than Temporal," which would be an
apples-to-oranges claim against a full distributed engine.
