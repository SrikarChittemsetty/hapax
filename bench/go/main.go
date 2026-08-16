// Command loadgo is a Go port of bench/load.py.
//
// The point is the comparison, so this deliberately measures the same things the
// same way: seed N tasks, release W concurrent workers at once, have each drain
// the queue by claiming a task and committing its side effect, and report
// throughput plus claim/commit percentiles. The exactly-once invariant is
// re-checked at every concurrency level, because a throughput number from a run
// that double-charged is worth less than no number at all.
//
//	go run . --conninfo "postgres://user@127.0.0.1:5432/mdt" --tasks 20000 --workers 1,2,4,8,16,32 --repeat 3
//
// # What is held identical to the Python harness
//
// The SQL. Both harnesses issue byte-for-byte the same claim statement and the
// same commit-with-effect sequence, copied from src/hapax/postgres.py rather
// than reimplemented, so any difference in the numbers is a difference in the
// client and not in what the database was asked to do.
//
// The measurement. Same split between "time to claim" and "time to do the work
// and commit", same percentile function (nearest-rank on a sorted slice), same
// readiness barrier before the clock starts, same median-of-repeats.
//
// # What cannot be held identical, and why it matters
//
// Python spawns W operating-system *processes*, each with one connection. Go
// runs W *goroutines* over a pool sized to W. That is not a detail I can
// engineer away — it is the difference between the two runtimes, and it is most
// of what the comparison is measuring. Concretely, Go avoids ~150 ms of
// interpreter startup per worker (which the barrier already excludes from the
// timed window), avoids per-operation Python object overhead, and shares one
// process's memory instead of W copies.
//
// So: this is a fair comparison of *how each language's natural concurrency
// model drives the same database*, not a controlled experiment isolating one
// variable. The README says so plainly.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const amount = 50

// claimSQL is copied verbatim from PostgresTaskStore.claim_task. The comment
// about coalesce is kept because it is the reason the query uses an index scan
// rather than a sequential scan plus sort — see bench/RESULTS.md.
const claimSQL = `
UPDATE tasks
   SET lease_expires_at = $1,
       attempts = attempts + 1
 WHERE id = (
       SELECT id FROM tasks
        WHERE state = 'working'
          -- coalesce, not "IS NULL OR ...", so this stays a plain filter the
          -- planner can apply while walking idx_tasks_claim_order in
          -- created_at order.
          AND coalesce(lease_expires_at, '-infinity'::timestamptz) < $2
        ORDER BY created_at
          FOR UPDATE SKIP LOCKED
        LIMIT 1
       )
RETURNING id, state`

// The three statements complete_with_effect runs inside one transaction: lock
// the row, apply the side effect, move the task to completed. Splitting them the
// same way keeps the transaction boundaries identical to the Python version.
const (
	lockSQL   = `SELECT id, state FROM tasks WHERE id = $1 FOR UPDATE`
	effectSQL = `INSERT INTO ledger (task_id, amount) VALUES ($1, $2) ON CONFLICT (task_id) DO NOTHING`
	completeSQL = `UPDATE tasks SET state = 'completed', result = $2, updated_at = $3 WHERE id = $1`
)

type report struct {
	workers       int
	tasks         int
	completed     int
	wall          time.Duration
	throughput    float64
	claimP50      float64
	claimP99      float64
	workP50       float64
	workP99       float64
	ledgerRows    int
	ledgerTotal   int
	unfinished    int
	exactlyOnce   bool
	throughputMin float64
	throughputMax float64
}

// percentile uses nearest-rank on a sorted slice — the same rule as the Python
// harness's percentile(), so the two sets of percentiles are comparable.
func percentile(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	idx := int(float64(len(sorted)) * p)
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return sorted[idx]
}

// worker drains the queue, timing each claim and each commit separately.
// Splitting them is the diagnostic: if throughput stops scaling, either workers
// are queueing to *get* work or queueing to *finish* it, and only measuring them
// apart tells you which.
func worker(ctx context.Context, pool *pgxpool.Pool, leaseSeconds float64, start <-chan struct{}) (claims, works []float64, done int, err error) {
	// Take the connection before the barrier so connection setup is outside the
	// timed window, matching the Python harness where each process has already
	// connected before it signals ready.
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return nil, nil, 0, err
	}
	defer conn.Release()

	<-start

	for {
		t0 := time.Now()
		now := time.Now().UTC()
		deadline := now.Add(time.Duration(leaseSeconds * float64(time.Second)))

		var taskID, state string
		row := conn.QueryRow(ctx, claimSQL, deadline, now)
		claimErr := row.Scan(&taskID, &state)
		claims = append(claims, float64(time.Since(t0).Microseconds())/1000.0)
		if claimErr == pgx.ErrNoRows {
			return claims, works, done, nil // queue drained
		}
		if claimErr != nil {
			return claims, works, done, claimErr
		}

		t1 := time.Now()
		if err := completeWithEffect(ctx, conn, taskID); err != nil {
			return claims, works, done, err
		}
		works = append(works, float64(time.Since(t1).Microseconds())/1000.0)
		done++
	}
}

// completeWithEffect mirrors PostgresTaskStore.complete_with_effect: the side
// effect and the state change commit in ONE transaction, which is what makes the
// effect exactly-once rather than merely idempotent.
func completeWithEffect(ctx context.Context, conn *pgxpool.Conn, taskID string) error {
	tx, err := conn.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx) //nolint:errcheck // no-op once committed

	var id, state string
	if err := tx.QueryRow(ctx, lockSQL, taskID).Scan(&id, &state); err != nil {
		return err
	}
	if state == "completed" {
		// Idempotent recovery: the effect already committed. Do not run it again.
		return tx.Commit(ctx)
	}
	if _, err := tx.Exec(ctx, effectSQL, taskID, amount); err != nil {
		return err
	}
	result := fmt.Sprintf(`{"charged": %d}`, amount)
	if _, err := tx.Exec(ctx, completeSQL, taskID, result, time.Now().UTC()); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func runLevel(ctx context.Context, conninfo string, workers, tasks int, leaseSeconds float64) (report, error) {
	cfg, err := pgxpool.ParseConfig(conninfo)
	if err != nil {
		return report{}, err
	}
	// One connection per worker, so the database sees the same number of
	// concurrent clients as the Python harness's W processes.
	cfg.MaxConns = int32(workers)
	cfg.MinConns = int32(workers)

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return report{}, err
	}
	defer pool.Close()

	if _, err := pool.Exec(ctx, "TRUNCATE tasks, ledger"); err != nil {
		return report{}, err
	}

	// Seed. Uses the same idempotency-key shape as the Python harness so the
	// unique index does the same amount of work.
	seed := `INSERT INTO tasks (id, state, input, idempotency_key, created_at, updated_at)
	         VALUES ($1, 'working', '{"op":"charge"}'::jsonb, $2, now(), now())`
	batch := &pgx.Batch{}
	for i := 0; i < tasks; i++ {
		batch.Queue(seed, fmt.Sprintf("task_go%011d", i), fmt.Sprintf("load-%d-%d", workers, i))
	}
	if err := pool.SendBatch(ctx, batch).Close(); err != nil {
		return report{}, fmt.Errorf("seeding: %w", err)
	}

	start := make(chan struct{})
	var wg sync.WaitGroup
	var mu sync.Mutex
	allClaims := make([]float64, 0, tasks+workers)
	allWorks := make([]float64, 0, tasks)
	completed := 0
	var firstErr error

	ready := make(chan struct{}, workers)
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			// Signal readiness once the connection is in hand, then block on the
			// barrier — same shape as the Python harness's ready/go files.
			gate := make(chan struct{})
			go func() { ready <- struct{}{}; <-start; close(gate) }()
			c, k, d, err := worker(ctx, pool, leaseSeconds, gate)
			mu.Lock()
			defer mu.Unlock()
			allClaims = append(allClaims, c...)
			allWorks = append(allWorks, k...)
			completed += d
			if err != nil && firstErr == nil {
				firstErr = err
			}
		}()
	}
	for i := 0; i < workers; i++ {
		<-ready
	}

	t0 := time.Now()
	close(start)
	wg.Wait()
	wall := time.Since(t0)

	if firstErr != nil {
		return report{}, firstErr
	}

	sort.Float64s(allClaims)
	sort.Float64s(allWorks)

	var ledgerRows, ledgerTotal, unfinished int
	if err := pool.QueryRow(ctx,
		"SELECT count(*), coalesce(sum(amount), 0) FROM ledger").Scan(&ledgerRows, &ledgerTotal); err != nil {
		return report{}, err
	}
	if err := pool.QueryRow(ctx,
		"SELECT count(*) FROM tasks WHERE state <> 'completed'").Scan(&unfinished); err != nil {
		return report{}, err
	}

	return report{
		workers:     workers,
		tasks:       tasks,
		completed:   completed,
		wall:        wall,
		throughput:  float64(completed) / wall.Seconds(),
		claimP50:    percentile(allClaims, 0.50),
		claimP99:    percentile(allClaims, 0.99),
		workP50:     percentile(allWorks, 0.50),
		workP99:     percentile(allWorks, 0.99),
		ledgerRows:  ledgerRows,
		ledgerTotal: ledgerTotal,
		unfinished:  unfinished,
		exactlyOnce: ledgerRows == tasks && ledgerTotal == tasks*amount && unfinished == 0,
	}, nil
}

func main() {
	var (
		conninfo = flag.String("conninfo", "", "postgres URL, e.g. postgres://user@127.0.0.1:5432/mdt")
		tasks    = flag.Int("tasks", 20000, "tasks seeded per level")
		workers  = flag.String("workers", "1,2,4,8,16,32", "comma-separated worker counts")
		lease    = flag.Float64("lease-seconds", 60, "lease a worker takes when claiming")
		repeat   = flag.Int("repeat", 1, "runs per level; the median is reported")
	)
	flag.Parse()

	if *conninfo == "" {
		fmt.Fprintln(os.Stderr, "--conninfo is required")
		os.Exit(2)
	}

	levels := []int{}
	for _, s := range strings.Split(*workers, ",") {
		n, err := strconv.Atoi(strings.TrimSpace(s))
		if err != nil {
			fmt.Fprintf(os.Stderr, "bad worker count %q: %v\n", s, err)
			os.Exit(2)
		}
		levels = append(levels, n)
	}

	ctx := context.Background()
	fmt.Printf("load [go]: %d tasks per level, worker counts %v, median of %d\n\n", *tasks, levels, *repeat)
	fmt.Printf("%7s %9s %10s %10s %9s %9s %7s  correct\n",
		"workers", "tasks/s", "claim p50", "claim p99", "work p50", "work p99", "wall")
	fmt.Println(strings.Repeat("-", 78))

	reports := []report{}
	allOK := true
	for _, w := range levels {
		runs := []report{}
		for r := 0; r < *repeat; r++ {
			rep, err := runLevel(ctx, *conninfo, w, *tasks, *lease)
			if err != nil {
				fmt.Fprintf(os.Stderr, "level %d failed: %v\n", w, err)
				os.Exit(1)
			}
			runs = append(runs, rep)
		}
		sort.Slice(runs, func(i, j int) bool { return runs[i].throughput < runs[j].throughput })
		rep := runs[len(runs)/2]
		rep.throughputMin = runs[0].throughput
		rep.throughputMax = runs[len(runs)-1].throughput
		for _, r := range runs {
			if !r.exactlyOnce {
				rep.exactlyOnce = false
			}
		}
		reports = append(reports, rep)
		allOK = allOK && rep.exactlyOnce

		correct := "yes"
		if !rep.exactlyOnce {
			correct = fmt.Sprintf("NO — %d rows", rep.ledgerRows)
		}
		fmt.Printf("%7d %9.1f %9.3fm %9.3fm %8.3fm %8.3fm %6.2fs  %s\n",
			rep.workers, rep.throughput, rep.claimP50, rep.claimP99,
			rep.workP50, rep.workP99, rep.wall.Seconds(), correct)
	}

	best := reports[0]
	for _, r := range reports {
		if r.throughput > best.throughput {
			best = r
		}
	}
	fmt.Printf("\npeak throughput %.1f tasks/s at %d workers\n", best.throughput, best.workers)

	fmt.Println("\nscaling relative to one worker:")
	base := reports[0]
	for _, r := range reports {
		speedup := r.throughput / base.throughput
		ideal := float64(r.workers) / float64(base.workers)
		fmt.Printf("  %3d workers: %5.2fx  (perfect scaling would be %.0fx, efficiency %.0f%%)\n",
			r.workers, speedup, ideal, 100*speedup/ideal)
	}

	if !allOK {
		fmt.Println("\nCORRECTNESS FAILURE — a throughput number from a run that double-charged is worthless")
		os.Exit(1)
	}
}
