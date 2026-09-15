# Wave G scanner performance correction

**Branch:** `fix/wave-g-scanner-performance`
**Required base:** `f0553d35e06dd49434b771af378ec7b49aaf80c1`
**Data class:** deterministic fixture/demo synthetic provider data, not a real-provider SLA

## Root cause

The failed owner-Windows candidate already exposed stage/provider attribution. Source inspection and
the synthetic stress topology confirmed the critical-path problem:

1. fixture clusters were scanned one at a time;
2. Matchbook, Polymarket and Kalshi market discovery ran serially inside each cluster;
3. Polymarket/Kalshi depth calls ran serially per market/runner; and
4. each provider call could consume its bounded timeout before the next independent call began; and
5. bulk event matching re-read enabled mapping rules for every candidate event pair.

For `N` fixtures with controlled latency `L`, the representative Matchbook/Polymarket workload had a
serial provider floor of:

```text
one-time discovery + N × (Matchbook markets + Polymarket markets + two Polymarket books)
3L + N × 4L
```

At dozens of fixtures this explains the observed near-linear owner-Windows growth. This correction
does not increase the 45s collector budget, 25s HOT budget, 60s browser timeout, or provider timeout.

## Bounded-concurrency design

- Up to 8 fixture clusters are active.
- Per-provider semaphores are explicit and configurable:
  - Matchbook: 4;
  - Polymarket: 8;
  - Kalshi: 4.
- Independent venue sides within a cluster are fetched concurrently.
- Independent outcome/order-book calls are fetched concurrently within provider limits.
- Results are written into their original cluster index and merged in deterministic input order.
- Existing canonical clustering, one-to-one greedy market pairing, strict settlement equivalence,
  quote freshness, depth, fee/FX/risk, solver/allocation and paper-only semantics are unchanged.
- Deadline leftovers remain `not_evaluated_scan_deadline`; provider failures remain degraded/partial.
- Provider tasks retain bounded timeout, cancellation drain, orphan and live-task diagnostics.
- Diagnostics now expose configured cluster/provider limits and observed peak provider concurrency.
- HOT with known source events still bypasses `list_events`; it scans only the requested identities.
- Bulk event matching snapshots enabled deterministic mapping rules once, then uses a conservative
  confidence upper-bound prefilter. The prefilter cannot reject a pair capable of reaching the
  existing match threshold.

Mapping Verify does not perform a ChatGPT/OpenAI call in collection. Deterministic equivalence remains
required in `MarketMatcher`; operator evidence packaging runs after the executable branch's economics
and allocation work. Human prompt generation remains in the separate mapping-review endpoint/UI.

## Deterministic synthetic benchmark

Assumptions:

- fixed 40ms `asyncio.sleep` per provider call;
- each fixture has one Matchbook BTTS market, one Polymarket BTTS market and two Polymarket token
  books;
- event discovery calls all three configured providers once;
- all quotes are generated fixture/demo payloads with current synthetic timestamps;
- serial bound is the formula above, not a claim about real provider latency;
- timings below were captured locally on the exact implementation during the PR run.

| Fixtures | Wall clock | Serial provider bound | Relative speedup | Cluster scan | Peak provider tasks |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.126s | 0.280s | 2.22× | 84ms | 3 |
| 4 | 0.129s | 0.760s | 5.88× | 86ms | 8 |
| 16 | 0.319s | 2.680s | 8.40× | 257ms | 12 |
| 50 | 0.969s | 8.120s | 8.38× | 827ms | 12 |

The 50-fixture synthetic normal-latency workload therefore has material headroom against the 30s HOT
cadence. It demonstrates bounded scaling, not a production SLA.

### Stalled provider

With 16 fixtures, Matchbook `list_markets` deliberately sleeping for 30s, a 0.8s per-call timeout and
a 2.5s cycle budget:

- wall clock: 2.003s;
- Matchbook health: `degraded`;
- 0 clusters falsely marked evaluated;
- 8 deadline leftovers plus degraded/unavailable results for already-started work;
- 11 timed-out provider calls;
- no fabricated qualifying opportunity.

This is truthful partial/degraded behavior; healthy provider work still returns. The stalled sleep is
cancelled rather than awaited for 30s.

### Repeated HOT soak

Five repeated 8-fixture HOT cycles after one UNIVERSE discovery:

```text
0.137s, 0.138s, 0.136s, 0.136s, 0.136s
```

Matchbook/Polymarket/Kalshi `list_events` counts remained `1/1/1` (the initial UNIVERSE cycle only).
Every HOT cycle reported zero orphaned/live provider tasks. There is no progressive slowdown in this
synthetic soak.

## Attribution

Every report retains provider totals and these stages:

- `event_lookup`;
- `market_discovery`;
- `book_depth`;
- `mapping_equivalence`;
- `fees_fx_risk`;
- `solver_allocation`;
- `current_state_finalization`;
- `persistence` (coordinator-owned).

Elapsed stage totals are attribution (parallel call time can exceed wall clock), while top-level
`event_discovery_ms`, `cluster_scan_ms`, `assembly_ms` and `total_ms` remain wall-clock measures.

The 60-fixture manual diagnostic regression measured `normalize_match` at about 3.6s before the
mapping-rule snapshot and 66ms after it on the local runner. This is synthetic/test timing, not a
real-provider claim; it records the CPU-side cause isolated by exact-head CI.

## Deferred evidence

- Issue #170 credentialed real-provider benchmark remains separate. It requires owner credentials and
  must report provider-specific observations rather than turning this synthetic result into an SLA.
- Owner-Windows repeated Fast Scan / Full Sweep re-smoke remains required.
- Architect review is required before merge.
