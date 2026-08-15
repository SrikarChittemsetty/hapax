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

## Roadmap

The intended next comparison is a **Temporal-wrapped baseline** running the same
task lifecycle, to answer "doesn't Temporal already do this?" with a measured
latency/throughput delta rather than an assertion. Note the honest framing: the
goal there is *comparable latency at a fraction of the operational footprint* for
this specific workload — not "faster than Temporal," which would be an
apples-to-oranges claim against a full distributed engine.
