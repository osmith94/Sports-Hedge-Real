# Dual-cadence scanner — architecture and implementation plan

**Issue:** #158
**Status:** Superseded for orchestration by Core Tenet 19.
**Date:** 14 September 2026 (implementation on #131 `86afb600`)

> **HISTORICAL SPEC — Core Tenet 19 is authoritative.**
>
> The current scanner operating model is:
>
> `UNIVERSE discovery → durable approved-market catalogue → one price engine → HOT/BACKGROUND priority`.
>
> HOT is not a second broad-discovery scanner. Once exact approved native IDs are
> catalogued, routine HOT/BACKGROUND pricing must not rediscover the fixture,
> rematch the market, or re-prove registered equivalence. UNIVERSE and the price
> engine are independently scheduled and may overlap, using one bounded shared
> provider-access layer.
>
> Any text below that implies chunk-until-HOT scheduling, a global scan exclusion
> lock, HOT-wide rediscovery, exclusive HOT-only pricing, or fixture-wide
> completeness for unrelated market families is superseded by
> `docs/core-tenets/19_CONCURRENT_HOT_AND_UNIVERSE_SCANNING.md` and Core Tenet 20.
> The remaining historical material may still be useful for identity, radar TTL,
> and implementation provenance; it is not the orchestration contract.

This is a scanner/scheduler/read-model change. It does not add venue write, place, cancel, or sign paths. Phase 1 remains `SPORTS_HEDGE_MODE=paper` / `SPORTS_HEDGE_EXECUTION_ENABLED=false`.

## 1. Implementation base and stacking

Inspected 14 September 2026; dual-cadence decisions locked by architect review `5196716600`. Prerequisite #157 owner-Windows **PASSED**. Identity/current-state blocker **#160/#161 architect-PASS** and merged into #131.

| Object | Ref | Role |
| --- | --- | --- |
| #131 (current) | `cursor/paper-demo-consolidation-08fc` @ `292e8109cf2d34a23eb39b5efc4555514724537e` | Paper demo consolidation + #157 leftover/budget + **#161 `FixtureCurrentStateStore` identity seam**. **Implementation base.** |
| #161 / #160 (integrated) | merge commit is #131 `292e8109` | Process-memory canonical fixture current-state + identity aliases for Tracked click-through. **Reuse this store; do not add a second identity system.** |
| #157 (integrated) | merge commit `3de14fc6` | Partial live-scan finalisation before the 50s hard timeout. Reuse leftover/budget; do not rewrite. |
| This document | docs-only, stacked on current #131 | Dual-cadence plan with accepted decisions encoded |

Original #131 implementation base included:

- 45s collector soft budget for **explicit collect**, 4s leftover-assembly reserve, 5s coordinator grace (#157). Wave G later replaced the browser-facing manual contract with a 20s bounded diagnostic; see `SCANNER_SYNTHETIC_VALIDATION.md`.
- `FixtureCurrentStateStore` (process memory, coordinator-owned) with `replace_from_report()` generation replace and explicit identity aliases (`resolve_canonical_id` / `identities_for`). Drill-down already reads this store. Tracked is **still** latest-completed-cohort via `last_report().paper_decisions`.
- The store’s own docstring already names dual-cadence (#158/#159) as the next upsert/TTL merge. Implementation must **extend** that class, not create `fixture_current_state.py` a second time.

Auto-loop HOT uses a **separate 25s** collector timeout (see §5.1). Explicit `POST /paper/collect` is now a bounded 20s diagnostic (+5s coordinator grace) and does not represent either scheduled lane.

## 2. Product decision

Change the Sports Hedge scan workflow from one monolithic cadence into **two coordinated scan lanes** so near-kickoff and live markets refresh frequently without forcing every future fixture through the same 30–60s cycle.

This is an owner-requested workflow change, not a bug workaround. Do not “fix” it by reducing `max_event_pairs` or stretching the HOT loop to minutes.

Prefer **one scheduler with two coordinated cohorts**, not two unrelated scanners fighting for providers or state.

## 3. Applicable tenets

Read before implementation (this pass already did):

| Tenet | Why it applies |
| --- | --- |
| 02 Paper mode | No venue write/place/cancel/sign. `execution_enabled=false`. |
| 03 Canonical equivalence | One identity store. No fuzzy joins across lanes. |
| 04 Arbitrage operations | Near ≠ triggered. Qualifying arbs from either lane surface. Stale/partial states stay visible. |
| 09 Capital / priority | No change to native pools; priority is scan scheduling only. |
| 11 UI / data honesty | Distinct Fast scan vs Full sweep. Stale rows must not look current. Empty stays empty. Unknown in-play is not labelled live. |
| 12 Agent review | This document + PR template. |
| 14 Event-driven dislocation | Fast lane is the Phase 1 realisation of “increase snapshot frequency for affected/urgent events within rate limits.” Burst scheduler stays a later overlay, not this PR. |
| 19 Concurrent workers | HOT and UNIVERSE must overlap in wall-clock time. This document’s leftover-until-HOT chunking is no longer the orchestration contract. |
| 15 Fees / FX | Both lanes run the same fail-closed economics. No invented costs. |

Non-applicable for this slice: 05–08, 10, 13, 16–18 except that paper-entry quote-age fail-closed (18/04) must not be weakened so distant Tracked rows can look live.

**Known conflict if mishandled:** keeping `Tracked = last_report.paper_decisions` after HOT-only cycles would hide distant fixtures *or*, if last_report is unioned carelessly, show expired HOT quotes as current. The contract must change explicitly (§6). Silently mixing stale rows is a Tenet 11 violation.

## 4. Current architecture (as of #157)

### 4.1 One loop, one lock, one report

`LiveRefreshCoordinator` (`backend/src/sports_hedge/application/live_refresh.py`):

- Single `interval_seconds` (default 30, settings `paper_live_refresh_interval_seconds`, clamp 15–300).
- Single `asyncio.Lock` around `run_cycle()`.
- Single `_last_report: CollectionReport`. `record_report()` still **replaces** status fixtures, paper decisions, venue health, and operator summary from that cycle.
- Coordinator-owned `FixtureCurrentStateStore` (`application/fixture_current_state.py`, #161). `replace_from_report()` atomically replaces the current **collection generation** and records identity aliases (cluster id, source event ids, paper-decision event ids). `fixture_detail` / `fixture_identities` resolve through that alias map. This is still a latest-generation snapshot, not yet a HOT/UNIVERSE TTL merge.
- Server loop: tick → sleep `interval_seconds`. If `cycle_in_progress`, it waits 1s then **breaks** (does not overlap). There is no preemption inside a running collect.
- Cycle timeout: `paper_scan_cycle_timeout_seconds` (45) + `SCAN_CYCLE_RETURN_GRACE_SECONDS` (5).

`server_owned_refresh_tick` and `POST /paper/collect` both call `_execute_collection()` → `ReadOnlyCrossVenueCollector.collect_and_scan()` with the same kwargs (`PaperCollectionRequest`, default `max_event_pairs=60`).

### 4.2 Collector work shape

Each cycle, independently of fixture urgency:

1. `list_events` on Matchbook, Polymarket, Kalshi (venue-union discovery).
2. In-scope filter + normalize + `cluster_venue_events` (canonical fixture identity; `max_event_pairs` never drops a multi-venue cluster).
3. Sequential `_scan_cluster` (markets + books + paper scan) until soft deadline.
4. Leftovers appended as `market_evaluation_state=not_evaluated_scan_deadline`.
5. Persist every `paper_decision` into audit + watchlist.

Cluster order today is `venue_count` descending (`fixture_clusters.py`). There is **no** kickoff / in-play / opportunity ranking in the live collector. The dislocation burst scheduler (`arbitrage/dislocations/scheduler.py`) is a separate ranking utility and is **not** wired into `collect_and_scan`. v1 must not couple it in.

In-play truth is Matchbook-only (`matchbook_fixture_state`): `in_running` from payload flags; live scores only when both numeric scores are present. Missing in-play is `None`, never inferred from prices.

### 4.3 Tracked = latest completed cohort

This is the live contract, not an accident.

`GET /paper/watchlist/tracked` (`backend/src/sports_hedge/api/watchlist.py`):

```text
report = coordinator.last_report()
if report is None: return []
cohort_ids = tracked_cohort_opportunity_ids(report.paper_decisions)
return service.tracked(..., collection_cohort_ids=cohort_ids)
```

`WatchlistService.tracked` documents: *“Current collection-cohort board … Pass collection_cohort_ids from the latest completed live collection's paper decisions. An empty set is an honest empty current snapshot.”*

Tests in `backend/tests/test_tracked_current_snapshot.py`:

- Tracked empty until a live collection completes (history may exist; board stays empty).
- Second completed collection **drops** markets that were only in the previous cohort (`mkt-a-only` leaves Tracked, persists in activity/history).
- A degraded completed refresh shows **only that cohort** (`mkt-healthy`; previous `mkt-previous` disappears).

`Near` / `Triggered` do **not** use the cohort filter. They freshness-filter the whole SQLite watchlist. `effective_quote_age_ms` adds wall-clock since `last_seen_at` to stored quote age. Default `max_quote_age_ms=1000`. So TRIGGERED fail-closes ~1s after last observation unless the clock is frozen (tests) or the operator reads immediately. Paper entry keeps that fail-closed gate. Dual cadence must not loosen it.

### 4.4 Operator status

One ambiguous `Last scan`:

- `LiveRefreshStatus.last_completed_at` / `last_duration_ms` / `interval_seconds` / `cycle_in_progress` / `discovered_fixtures`.
- Frontend: `venue-health-bar.tsx` (`Last scan {relativeTime}`), `run-paper-scan.tsx` (`Last scan … · next … · cadence Ns`).

Fixture drill-down (`GET /paper/fixtures/{id}`) already uses `FixtureCurrentStateStore.detail()` with #161 aliases. Status `discovered_fixtures` still copies the last cycle list. A HOT-only `replace_from_report()` would still wipe UNIVERSE rows from the store — dual cadence must change that replace into a lane upsert (see §6.6 / §7 slice 2).

### 4.5 Why the single cycle fails the product

On owner-Windows, a 60-fixture universe can consume the entire 45s budget (#118 / #157). Distant weekend cards and the 15:00 in-play window compete equally. Tenet 14 requires prioritisation under high-liquidity concurrency. The current seam for that prioritisation is missing in the live collector.

A 45s HOT collector budget would also fail the 30s HOT cadence: one slow HOT cycle would miss the next due. That is correction **A** below.

## 5. Target architecture

```text
                    ┌──────────────────────────────────────────────┐
                    │      LiveRefreshCoordinator (one)            │
                    │  HOT due every 30s, timeout 25s, no overlap  │
                    │  UNIVERSE generation due 180s / budget 150s  │
                    │  each UNIVERSE run = chunk until             │
                    │    next_hot_due - safety_margin              │
                    └────────────┬─────────────────┬───────────────┘
                                 │                 │
                    Lane A HOT   │                 │  Lane B UNIVERSE
                    known IDs    │                 │  discovery + resume cursor
                                 ▼                 ▼
                    ReadOnlyCrossVenueCollector.collect_and_scan
                    (scan_lane, identity_scope, cycle_timeout,
                     leftover/budget rules from #157 unchanged)
                                 │
                                 ▼
                    CanonicalFixtureState
                    FixtureCurrentStateStore (#161, extend — do not fork)
                    key = canonical_event_id + identity aliases
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
        Tracked board      Near / Triggered    Operator status
        current-state      executable          Fast scan ≠ Full sweep
        merge + TTL        freshness ~1s       + per-fixture last_scanned
```

### 5.1 Lane A — HOT / fast loop

| Parameter | Accepted default |
| --- | --- |
| Cadence | 30 seconds (`paper_live_refresh_hot_interval_seconds`). Wall-clock from last HOT **due** (not “sleep 30s after a 25s run”). |
| Collector timeout | **25 seconds** (`paper_scan_hot_cycle_timeout_seconds`). Separate from the 20s manual diagnostic. |
| Envelope | #157 leftover reserve (4s, from the 25s) + coordinator grace (5s) ⇒ worst-case end-to-end **~30s**. Collector cluster work ≤ 21s. |
| Self-overlap | **Forbidden.** Do not start a second HOT while one is in progress. If a cycle hits the envelope, skip the missed slot and run the next due after return. |
| Cohort | (a) truthful provider in-play (`in_running is True`); (b) `kickoff_utc` in `(now, now + 60 minutes]`; (c) kickoff-passed + unknown in-play **only while** `(now - kickoff_utc) ≤ 3h` (§5.4); **(d) Issue #200:** any still-current fixture whose latest valid merged current-state proves a qualifying/executable arb, even if kickoff is days away. Kickoff/in-play are additional HOT reasons, not requirements that suppress a live arb. |
| In-play labelling | Provider `in_running is True` is a live label and is never inferred. Post-kickoff plus a successful current evaluation with matched equivalents still present may also show operator IN PLAY without writing `in_running` or live scores. Kickoff proximity / post-kickoff unknown without that evaluation may keep HOT **membership** but must not set `in_running`. A successful post-kickoff evaluation with zero matched equivalents leaves current HOT radar (`no_current_equivalent_markets_post_kickoff`) and does not fabricate completed. |
| Identity source | Canonical store (already-known IDs). Prefer `list_markets` / books for those events. Do **not** rediscover the whole world every 30s. |
| Priority inside the lane | Simple v1 key only (§5.5). Do **not** call `arbitrage/dislocations/scheduler.py`. |
| Output | Refresh executable market/book economics needed for qualification. Bound and return partial truthful state (#157 leftover rules). |
| Radar TTL | **90s**. Radar-current only. Executable quote freshness stays ~1s fail-closed. |

HOT may include a fixture that UNIVERSE has not fully evaluated yet if kickoff/in-play membership is known from inventory.

Normal hot cohorts should finish well under 25s. The 25s timeout exists so a slow HOT still finishes or aborts inside the 30s cadence instead of stealing the next slot.

### 5.2 Lane B — UNIVERSE / full sweep

| Parameter | Accepted default |
| --- | --- |
| Generation cadence | Persistent worker. After a **terminal-complete** generation, wait `paper_universe_discovery_interval_seconds` / operator `universe_cadence_seconds` (default **1800s**). Then start a **new generation id**. Incomplete generations resume/chunk/retry without this wait. Operators may pause this periodic timer (`universe_scans_paused`) without changing cadence or creating a second worker; BACKGROUND/HOT/ACTIVE TRADE continue. Fixture radar membership still uses `paper_live_refresh_universe_interval_seconds` (180s). BACKGROUND pricing uses `paper_background_price_interval_seconds` (90s) independently. |
| Generation work budget | **150 seconds** (`paper_scan_universe_generation_budget_seconds`). Accumulated collector time across chunks in one generation. **Not** one continuous 150s `collect_and_scan`. |
| Per-run chunk | Bound by `min(remaining_generation_budget, next_hot_due - now - safety_margin)`. Persist cursor, yield, let HOT run, resume. |
| Safety margin | `paper_universe_hot_yield_safety_margin_seconds` default **2s**. Chunk wall time must also leave #157 coordinator grace inside that bound (§5.2.1). |
| Cohort | Full currently captured/in-scope universe, including T+6d. |
| Purpose | Discover new fixtures/markets; keep distant fixtures on radar; detect initial cross-venue mispricing; promote into HOT as kickoff approaches. |
| Progress | Resumable cursor (`universe_cursor_canonical_event_id` + generation id) persisted in the local paper-settings SQLite checkpoint. Incomplete chunk retains evaluated work; remainder `not_evaluated_scan_deadline` for **this chunk**, without clobbering prior valid evaluations. Generation-local skip/cursor/successful-work is bound to `universe_generation_id` and **reset only when that generation genuinely completes** (`leftover_n=0`) or an explicit coordinator reset/invalid checkpoint proves it cannot resume. A provider timeout/exception does **not** consume successful-generation budget or erase cursor/evaluated IDs. If the 150s successful-work budget is exhausted with leftovers, **pause** until the next UNIVERSE continuation window and keep the same generation/cursor/evaluated IDs; resume later with a fresh 150s window. HOT preemption inside an open generation keeps skip/cursor. A newly due generation (after a real close) plans with empty skip/cursor and a new generation id. Completeness diagnostics distinguish deadline leftovers, a genuine empty universe, and stale-generation skip bugs (which the scheduler must not emit). |
| Radar TTL | **360s**. Radar-current only. |

UNIVERSE must never starve HOT. A generation continues via cursor until evaluated; HOT may run at the same time. After a terminal close the worker waits `paper_universe_discovery_interval_seconds` then starts a new generation id. Failed provider/series units use bounded backoff (`RETRY_WAIT`) and must not claim generation completeness or busy-loop.

### 5.2.1 UNIVERSE chunk wall-clock

Each scheduler tick that is not a HOT run:

```text
remaining_generation = generation_budget - generation_work_used          # 150s cap
until_hot = next_hot_due - now - safety_margin                          # default 2s
chunk_wall = min(remaining_generation, until_hot)

if chunk_wall < min_chunk:          # cannot fit 5s grace + 4s reserve + ≥1 provider wait
    skip UNIVERSE this slot; wait for HOT

collector_timeout = chunk_wall - SCAN_CYCLE_RETURN_GRACE_SECONDS        # 5s
# #157 reserve is taken from collector_timeout (min(4s, 20% of collector_timeout))
run collect_and_scan(scan_lane=universe, cycle_timeout=collector_timeout, resume_cursor=…)
on successful report: persist checkpoint + generation_work_used += successful_duration
on provider exception/timeout: do not charge successful-work; keep cursor/evaluated IDs; bounded backoff
if successful-work budget exhausted with leftovers: pause same generation until next continuation window
yield to HOT
```

`min_chunk` initial default: **6s** (5s grace + leftover-safe remainder). If the next HOT is closer than that, UNIVERSE does not start.

Do not pass `cycle_timeout_seconds=150` into a single collector call on the auto-loop.

### 5.3 Shared-state rules

1. **One canonical identity store.** Both lanes upsert the same `canonical_event_id` / `canonical_market_id` (existing `canonical_source_event_id` / `canonical_matched_market_id` / `watch:{canonical_market_id}`). No second ID system, no fuzzy join between lanes.
2. **HOT preempts UNIVERSE.** UNIVERSE must not monopolise provider concurrency, collector inflight tasks, or coordinator state when HOT is due. Chunks exist so preemption is the normal path, not an emergency cancel of a 150s job.
3. **Avoid duplicate calls.** UNIVERSE discovery maintains inventory. HOT refreshes known hot IDs. HOT may do a tiny identity repair (`list_events` for a missing kickoff/in-play flag) but not full pagination.
4. **Promote / demote automatically.** See §5.4. Do not invent “completed” or “live” from missing data or elapsed time alone. Issue #200: a UNIVERSE-discovered qualifying executable arb is promoted into subsequent HOT identity immediately, using known source events for repricing. Promotion is not permanent: when the merged current-state ceases to qualify, the fixture leaves opportunity-promoted HOT membership unless another lifecycle HOT reason still applies.
5. **Qualifying arbs from either lane surface immediately.** Persist watchlist observations during the producing cycle (including a UNIVERSE chunk). Do not buffer UNIVERSE TRIGGERED until the next HOT tick.
6. **Preserve** settlement equivalence, fees/FX/depth/risk fail-closed, paper-only venues, append-only audit.
7. **v1 store is the existing process-memory `FixtureCurrentStateStore`.** Restart: Tracked empty until a collection completes. **UNIVERSE generation 0 is due immediately** on startup — do not wait 180s. SQLite fixture-inventory persistence is out of scope. Do not add a second canonical store.

### 5.4 Promotion / demotion function

Pure, clock-injected, deterministic:

```text
classify_scan_lane(
    fixture,
    now,
    *,
    hot_horizon=60m,
    post_kickoff_unknown_horizon=3h,
    post_kickoff_current_radar_ceiling=4h,
) -> HOT | UNIVERSE | DROP

DROP when provider status is completed/settled/void/expired/finished/final/closed/graded
     (explicit payload only; never fabricate completed/live from elapsed time)
     Official Matchbook GET /events states: open, suspended, closed, graded.
     closed and graded are explicit Matchbook terminal event states.
     Matchbook is a trusted terminal-status source only when Matchbook
     supplied that lifecycle status (`fixture_status_source=matchbook`).
     `matchbook_matched` / cluster coverage is not Matchbook lifecycle authority.

HOT  when in_running is True                         # provider live label
     and (now - effective_kickoff) <= 4h current-radar ceiling
     or 0 < (kickoff_utc - now) <= hot_horizon       # pre-kickoff
     or (
          in_running is not True
          and kickoff_utc <= now
          and (now - kickoff_utc) <= post_kickoff_unknown_horizon
          and not DROP
          and not postponed/delayed/rescheduled
        )
        # kickoff-passed, in-play unknown: HOT membership.
        # Operator IN PLAY requires a successful evaluation with equivalents.
        # It must not write in_running. Zero equivalents after that evaluation
        # leave current HOT radar without fabricating completed.

UNIVERSE when the fixture is still current and not HOT
     including T-6d, T-4h, and explicit postponed/delayed/rescheduled
     (still not labelled completed or live)
     A T-6d (or other distant) fixture is HOT *identity* when the latest
     valid merged current-state proves a qualifying executable arb
     (Issue #200). That is opportunity promotion, not a change to this
     lifecycle function. Unknown/ambiguous settlement, stale quotes,
     missing executable depth, unknown fees/FX, allocator rejection,
     risk rejection, or insufficient venue equivalence do not promote.

DROP also when kickoff-passed + unknown beyond the 3h window (#164)
     Leaves current radar / HOT identity / Discovery inventory.
     Does not write fixture_status=completed or in_running=true.
     Explicit postponed/delayed/rescheduled is not dropped by kickoff arithmetic.

DROP also when a current/effective kickoff is available and the fixture is
     more than 4h past it (#275), even if stale Matchbook in_running=true
     or fixture_status=open persists. This is a hard football scheduling
     safety ceiling for current-radar/HOT membership only. Elapsed time
     must not fabricate completed/closed. Reason if surfaced:
     clock_expired_current_radar. Before 4h, genuine in-running remains HOT.
```

Provider `in_running is True` remains a live label and is never inferred. Inside the existing post-kickoff HOT window, a successful current evaluation with matched equivalents still present may also show operator IN PLAY without writing `in_running`. A successful evaluation with zero matched equivalents leaves current HOT radar then, without writing `fixture_status=completed`. After the 3h unknown window the fixture **leaves current radar** (not merely HOT scheduling). Elapsed time must not write `fixture_status=completed` or `in_running=true`. Explicit Matchbook/provider terminal status evicts immediately. A Matchbook-confirmed terminal tombstone must not be resurrected by a later Polymarket/Kalshi unknown or postponed/delayed/rescheduled observation. A later Matchbook `open` / `in-play` / `suspended` / `rescheduled` (or Matchbook `in_running=True` with a non-terminal status) may restore current radar.

### 5.5 HOT sort key (v1, required)

Simple, stable, no burst-module import:

```text
(
  0 if in_running is True else 1,   # truthful in-play first
  kickoff_utc,                      # nearest kickoff next
  -opportunity_rank,                # TRIGGERED > NEAR > matched > unmatched
  canonical_event_id,               # tie-break
)
```

Do not couple `arbitrage/dislocations/scheduler.py` into this change.

## 6. Tracked contract change (required)

### 6.1 What must change

The current rule **Tracked = paper decisions of the single latest completed `CollectionReport`** cannot survive two cadences.

If HOT completes with five in-play markets, today’s API would show only those five and drop every T-6d row. If we naively unioned the last HOT report with the last UNIVERSE report, a stale HOT quote from a finished match could sit next to a fresh UNIVERSE row with no way to tell which is current.

### 6.2 Replacement: current-state merge by identity

**Tracked is the current radar board**, not “whatever the last HTTP cycle happened to evaluate.”

For each `canonical_market_id` (opportunity id `watch:{id}`):

| Lane of the latest **valid** observation | Membership rule |
| --- | --- |
| HOT | Show the latest HOT observation while `now < last_scanned_at + hot_ttl` (**90s**). If TTL expires with no refresh, drop from Tracked (fail closed). History/activity remain. |
| UNIVERSE | Show the latest valid UNIVERSE observation while `now < last_scanned_at + universe_ttl` (**360s**) **and** the fixture is not currently HOT. When the fixture is HOT, HOT observation wins; do not display an older UNIVERSE book as if it were the live quote. |
| Either lane, TRIGGERED / qualifying | Persist immediately. Tracked includes it if the radar TTL for **that observation’s lane** still holds. `/near` and `/triggered` continue to apply **executable** quote-age (`max_quote_age_ms`, default 1000) so a 90s-old UNIVERSE TRIGGERED does **not** remain an executable arb. It can remain on Tracked as radar with `freshness_class` honest. |
| Leftover `not_evaluated` this chunk | Does **not** clobber a previous valid evaluated observation for that identity. Diagnostics record unevaluated-this-pass. Equivalent 0 remains reserved for actually evaluated empty sets (#153). |
| Expired / completed fixture | Leave Tracked. Do not carry last odds forward as current. DROP only from explicit provider status. |
| No completed collection yet (including fresh process) | Tracked stays `[]` (unchanged honesty). |

**Do not** treat an empty HOT cohort as “empty Tracked.” An empty HOT cycle with a live UNIVERSE generation still shows distant current-state rows.

**Do not** keep a market on Tracked merely because SQLite history exists (today’s activity-vs-board split stays).

### 6.3 Freshness classes (operator-visible, compact)

Per tracked row:

```text
scan_lane: hot | universe
last_scanned_at
next_due_at          # now + remaining cadence for that lane
freshness_class:
  executable         # passes existing max_quote_age_ms (~1s live quote)
  radar_current      # inside lane radar TTL (90s / 360s), not executable-fresh
  expired            # past TTL — must not be returned as current
```

`expired` rows are omitted from Tracked (fail closed), not shown as live with a small print footnote. Diagnostics/activity may still mention them.

Paper BET / 8F / priority alerts continue to require `executable`. Radar_current is watch-only. That is how distant fixtures stay on the board without looking like live arbs (Tenet 11).

### 6.4 Near / Triggered

Unchanged economically:

- TRIGGERED only when settlement, fees, FX, depth, risk, and **executable quote age** pass.
- A UNIVERSE qualifying arb is written in that **chunk** and appears on `/triggered` immediately; the next GET after ~1s wall-clock ages it out of TRIGGERED exactly as HOT does today.
- Do not invent a 180s or 360s executable window for far-future arbs. Survivability/freshness stays fail-closed. “Surfaces immediately” means **no lane delay**, not **quotes stay live for the sweep interval**.

### 6.5 Degraded / partial sweeps

Replace `test_degraded_completed_refresh_shows_only_that_cohort`:

- A degraded UNIVERSE **chunk** that evaluated `{healthy}` and leftover `{rest}` **keeps** previous valid current-state for `{rest}` if still inside universe TTL, and shows `{healthy}` from this chunk.
- Venue health for that **lane** is degraded/timeout as today.
- Leftover fixtures are listed in that lane’s `not_evaluated_count` / fixture inventory as `not_evaluated_scan_deadline` without wiping prior economics.

A failed lane (no report at all, hard timeout after #157 is supposed to have returned partial) must not blank the other lane’s current-state.

### 6.6 Fixture inventory / drill-down

`discovered_fixtures` on `GET /paper/live-refresh` must become the **canonical inventory snapshot** from `FixtureCurrentStateStore`, not the last cycle’s cluster list. Each row carries `scan_lane`, `last_scanned_at`, `market_evaluation_state`, `opportunity_state`.

Drill-down already resolves `canonical_event_id` **and** #161 aliases (`source_event_id`, paper-decision event ids, cluster id). Dual cadence must keep that alias map:

- HOT upserts only the fixtures it touched; it must **not** `replace_from_report()` the whole generation.
- UNIVERSE chunk upserts evaluated identities and records leftovers without deleting other current-state rows.
- `#161` click-through tests (`test_tracked_fixture_click_through.py`) must stay green: Tracked row identity still opens the same fixture via cluster id, source id, or decision id.

HOT cycles must not delete UNIVERSE rows.

## 7. Code seams (narrow)

Implement in this order on current #131 (`292e8109`). Do not start code in this PR.

| Slice | Seam | Change |
| --- | --- | --- |
| 0 | `#157` leftover/budget now on #131 | Call with per-run `cycle_timeout_seconds` (HOT 25s; UNIVERSE chunk derived from §5.2.1; explicit diagnostic 20s). |
| 1 | New `application/scan_lanes.py` | Pure `classify_scan_lane` (including 3h unknown bound), HOT sort key §5.5, TTL helpers. Clock injected. No dislocations import. |
| 2 | Existing `application/fixture_current_state.py` (#161) | **Extend, do not replace or fork.** Keep alias resolution. Change generation `replace_from_report` into lane upsert + §6 TTL merge so HOT cannot wipe UNIVERSE rows. **No SQLite inventory in v1.** |
| 3 | `LiveRefreshCoordinator` | Dual due-logic; HOT 30s / 25s / no self-overlap; UNIVERSE 180s generation / 150s work / chunk-until-HOT; preemption is chunk yield; nested status; startup UNIVERSE due immediately. `record_report` must upsert the existing store, not only `replace_from_report`. |
| 4 | `collect_and_scan(..., scan_lane, identity_scope, resume_cursor)` | HOT: known IDs from the store (via aliases), skip full pagination. UNIVERSE: discovery + resume. Reuse cluster scan + leftover. |
| 5 | `server_owned_refresh_tick` | If HOT due and not in progress → HOT. Else if UNIVERSE generation due or in-progress with remaining budget and chunk_wall ≥ min_chunk → UNIVERSE chunk. Explicit POST remains a separate bounded diagnostic (§8.2). |
| 6 | `GET /paper/watchlist/tracked` | Build cohort ids from current-state store, not `last_report.paper_decisions` only. Preserve #161 identity aliases for click-through. |
| 7 | `WatchObservation` / `NearOpportunity` / `DiscoveredFixture` | Additive: `scan_lane`, `last_scanned_at`, `next_due_at`, `freshness_class`. |
| 8 | `Settings` | See §9. |
| 9 | Frontend | Fast scan / Full sweep copy on health bar + scan note. Compact. Diagnostics stay Advanced. |
| 10 | Docs | This file + ADR 0002 + DEMO_READINESS Tracked section. |

**Do not touch:** treasury, allocator, unwind, venue order contracts, Research, dislocation burst engine, SQLite fixture inventory.

## 8. API / operator status

### 8.1 `GET /paper/live-refresh`

Additive nested objects. Keep legacy scalars as **HOT** aliases so old UI does not invent a blended timestamp.

```text
hot:
  cadence_seconds           # 30
  cycle_timeout_seconds     # 25
  cycle_in_progress
  last_started_at
  last_completed_at
  last_duration_ms
  next_due_at
  fixture_count             # current HOT membership
  evaluated_count
  not_evaluated_count
  last_error
  degraded                  # bool
universe:
  cadence_seconds           # 180 generation
  generation_budget_seconds # 150
  generation_work_used_s
  chunk_last_duration_ms
  … same progress fields …
  fixture_count             # inventory size
  evaluated_count           # this generation
  not_evaluated_count
  resume_cursor             # advanced/diagnostic
interval_seconds            # = hot.cadence_seconds (compat)
cycle_in_progress           # true if either lane in progress
last_completed_at           # = hot.last_completed_at (compat; UI must stop using this as the only line)
discovered_fixtures         # canonical inventory, not last cycle only
```

Operator copy (compact):

```text
Fast scan · 12s ago · 4.1s · next 18s · 7 hot · partial (2 not evaluated)
Full sweep · chunk 8s · gen 41/150s · next HOT in 18s · 104 universe · 60 evaluated / 44 not evaluated
```

Do not ship a single `Last scan` once both lanes exist.

### 8.2 Manual HOT and full diagnostic collection

The primary operator **Run scan** action calls `POST /paper/collect/hot`. It uses
the same current HOT identity scope, retained source events, HOT venue
participation, 25s collector timeout and 5s coordinator grace as server-owned
Fast Scan. A non-null empty HOT scope remains empty and must not trigger
universe-wide discovery.

`POST /paper/collect` remains a **UNIVERSE-shaped full diagnostic**, but now
uses a 20s collector timeout plus 5s coordinator grace, may return truthful
partial coverage, and omits redundant nested market inventory from its HTTP
response. The UI exposes it only as Advanced **Run full diagnostic**. This Wave
G correction supersedes the earlier 45s browser-facing contract.

When `PAPER_LIVE_REFRESH_ENABLED=true`, the server-owned HOT/UNIVERSE loop is the
only automatic collection owner. Browser auto-refresh polls
`GET /paper/live-refresh` and does not POST either collection endpoint. Manual
HOT and full diagnostic collection must not consume or resume scheduled
generation work (`generation_work_used_s`, cursor, due times). If a scheduled
lane is in progress, either manual action fails fast (409) rather than waiting
behind it.

Rationale: the failed candidate's owner-observed Fast Scan was about 58s and
Full Sweep was about 148/150s with 53 fixtures. The primary action now expresses
HOT intent and the advanced diagnostic returns partial state with margin before
the frontend abort, while bounded cluster/provider concurrency corrects the
serial market/depth topology for every lane. Giving a full diagnostic the 150s
generation budget remains a separate decision.

### 8.3 Watchlist

`GET /paper/watchlist/tracked` semantics change as in §6. Response rows gain additive freshness fields. No new identity prefix.

## 9. Settings

| Setting | Default | Notes |
| --- | --- | --- |
| `paper_live_refresh_hot_interval_seconds` | 30 | ge 15, le 60. Wall-clock HOT cadence. |
| `paper_background_price_interval_seconds` | **90** | Env fallback for BACKGROUND price-engine cadence. ge 30, le 300. Runtime operator authority is `background_cadence_seconds` (default 90, 60–600). Not UNIVERSE discovery. |
| `paper_universe_discovery_interval_seconds` | **1800** | Env fallback for post-terminal UNIVERSE discovery gap. ge 60, le 3600. Runtime operator authority is `universe_cadence_seconds` (default 1800, 60–3600). Not a between-chunk sleep, radar TTL, or generation budget. |
| `paper_universe_worker_cooldown_seconds` | **8** | Intra-generation pause only for incomplete chunk continuation. ge 5, le 15. Not discovery cadence and not BACKGROUND pricing. |
| `paper_live_refresh_universe_interval_seconds` | 180 | Fixture radar / membership interval. ge 60, le 300. Not BACKGROUND pricing and not UNIVERSE discovery. |
| `paper_live_refresh_interval_seconds` | 30 | **Alias of HOT.** Keep for env/launcher compat. |
| `paper_scan_hot_cycle_timeout_seconds` | **25** | Auto-loop HOT collector timeout. |
| `paper_scan_cycle_timeout_seconds` | 45 | General collector fallback; scheduled lane plans pass their own timeout. |
| `paper_scan_manual_diagnostic_timeout_seconds` | **20** | Explicit `POST /paper/collect` collector bound (+5s coordinator grace). |
| `paper_scan_universe_generation_budget_seconds` | **150** | Accumulated UNIVERSE work per generation. |
| `paper_universe_hot_yield_safety_margin_seconds` | 2 | Chunk bound: `next_hot_due - now - margin`. |
| `paper_hot_pre_kickoff_horizon_minutes` | 60 | |
| `paper_hot_post_kickoff_unknown_horizon_hours` | **3** | Unknown in-play leaves HOT after this; no fabricated completed/live. |
| `paper_hot_post_kickoff_current_radar_ceiling_hours` | **4** | Hard football current-radar/HOT ceiling after effective kickoff. Stale `in_running=true` / `open` cannot keep a fixture HOT forever. No fabricated completed/closed. |
| `paper_hot_current_state_ttl_seconds` | **90** | Radar TTL |
| `paper_universe_current_state_ttl_seconds` | **360** | Radar TTL |
| venue/provider timeouts | 15 / 8 | Unchanged |
| scanner cluster concurrency | 8 | Bounded active clusters. |
| Matchbook / Polymarket / Kalshi concurrency | 4 / 8 / 4 | Explicit per-provider semaphore caps. |

There is **no** `paper_scan_universe_cycle_timeout_seconds=150` on the auto-loop. Per-chunk timeout is derived (§5.2.1).

Windows launcher keeps `PAPER_LIVE_REFRESH_ENABLED=true`. No new execution flags.

## 10. Concurrency and rate-limit risks

| Risk | Why | Mitigation |
| --- | --- | --- |
| HOT 45s budget misses 30s cadence | Original proposal reused explicit-collect 45s | **25s HOT timeout** + 4s reserve + 5s grace ≈ 30s envelope. No HOT self-overlap. |
| 150s monolithic UNIVERSE continuously preempted / starves HOT | HOT due every 30s | **Chunk** UNIVERSE to `next_hot_due - safety_margin`; persist cursor; resume after HOT. 150s is generation work, not one job. |
| Two `collect_and_scan` overlap | New HTTP clients per cycle; doubles QPS | Shared `ProviderGate`. HOT wins. UNIVERSE is not running during HOT. |
| HOT rediscovers universe | 30s × full Gamma/Kalshi pagination | HOT `identity_scope` from inventory; UNIVERSE owns pagination. |
| UNIVERSE leftover wipes HOT books | `record_report` replaces `_last_report` | Stop using `_last_report` as Tracked source. Lane reports upsert the store. |
| SQLite watchlist writers | Two lanes persist decisions | Same repository as today; serialize persist on the coordinator (append-only, short). Do not hold the provider gate during persist. |
| `#157` cancel/uncooperative HTTP | Still required per chunk | Chunk yield uses the same leftover assembly; do not block on `aclose()`. |
| Stale unresolved fixtures in HOT forever | Kickoff-passed + unknown in-play | **3h** bound; then UNIVERSE only; no fabricated completed/live. |
| Stale Matchbook `in_running=true` / `open` remaining HOT forever | Provider flag never clears after kickoff | **4h** hard current-radar ceiling (#275); clock-expired drop; postponed/rescheduled still follow provider truth. |
| Burst scheduler unused | Separate module | **v1 must not import it.** Simple key §5.5 only. |
| Restart empty inventory | Process memory | Tracked empty until collect; **immediate UNIVERSE bootstrap**. |

Provider timeouts stay #157-bounded (`remaining soft budget`, `MIN_PROVIDER_WAIT_SECONDS`) on whatever collector timeout that run was given (25s HOT or derived chunk).

## 11. Migration / backward compatibility

| Surface | Compat |
| --- | --- |
| Env `PAPER_LIVE_REFRESH_INTERVAL_SECONDS` | Continues to set HOT cadence. |
| `LiveRefreshStatus.interval_seconds` / `last_completed_at` | Remain, documented as HOT aliases. UI must switch in the same implementation PR. |
| `GET /paper/watchlist/tracked` | **Breaking semantics**, additive fields. Same path. Tests in `test_tracked_current_snapshot.py` must be rewritten to the merge/TTL rules, not deleted. |
| Watchlist SQLite | Additive columns or JSON sidecar on opportunity; old rows: `scan_lane=null` treated as `universe` with `last_seen_at` as `last_scanned_at`. Missing lane + age > universe TTL → omit from Tracked (fail closed). |
| `#157` hang/partial tests now on #131 | Must stay green. Do not change leftover reason strings. |
| Explicit diagnostic / 60-pair cap | 20s bounded partial response; does not advance scheduler state. |
| Restart | Open UNIVERSE generation checkpoint in the local paper-settings SQLite restores generation id, successful-work accounting, cursor, evaluated IDs, and the last successful partial roster. Empty checkpoint: Tracked empty until a collection completes; scheduler still marks UNIVERSE due **immediately** (bootstrap), not after 180s. Watchlist history remains. Explicit coordinator reset clears the checkpoint. |

No data backfill job. No second canonical ID migration. No SQLite fixture inventory in this implementation.

## 12. Deterministic acceptance tests

New module `backend/tests/test_dual_cadence_scheduler.py` (clock injected; no live HTTP). Fixtures at **T-6d, T-4h, T-59m, kickoff, in-play, T+3h unknown, completed**.

| # | Assertion |
| --- | --- |
| 1 | Classifier: T-6d and T-4h → UNIVERSE; T-59m → HOT; kickoff-passed + `in_running True` → HOT live; kickoff-passed + `in_running None` within 3h → HOT membership, `in_running` stays None; completed status → DROP. |
| 2 | T-59m and in-play refresh on HOT cadence without waiting for UNIVERSE (advance clock 30s; HOT ran; UNIVERSE chunk did not have to finish). |
| 3 | T-6d is present on Tracked after a UNIVERSE chunk. A T-6d fixture **without** a current qualifying executable arb is **not** in HOT identity_scope. A T-6d fixture whose latest valid merged current-state proves a qualifying executable arb **is** in the next HOT identity_scope; HOT then uses known source events and does not rediscover the universe (Issue #200). |
| 4 | UNIVERSE chunk yields at `next_hot_due - safety_margin`; HOT starts on time; UNIVERSE **cursor advances** across **repeated** HOT cycles; generation_work_used increases each chunk; HOT is not starved. |
| 5 | Far-future qualifying paper_decision from a UNIVERSE chunk appears on `/triggered` and Tracked in that same chunk (`freshness_class=executable` at `as_of=observed_at`). |
| 6 | Clock advance from T-61m to T-59m promotes the fixture into HOT membership automatically. |
| 7 | Partial UNIVERSE chunk: evaluated ids keep economics; leftovers `not_evaluated_scan_deadline`; previous valid current-state for a leftover id is retained; `not_evaluated_count` explicit. |
| 8 | Same `canonical_event_id` / `canonical_market_id` across lanes; no duplicate Tracked rows. |
| 9 | `GET /paper/live-refresh` has distinct hot/universe status; Fast/Full fields independently true. |
| 10 | HOT TTL (90s) expiry drops a vanished in-play row from Tracked; activity history remains. UNIVERSE TTL (360s) expiry drops a T-6d row rather than showing it current. |
| 11 | Empty HOT cycle does not empty Tracked of in-TTL UNIVERSE rows. |
| 12 | `execution_enabled=false`; collector/venue modules still have no place/cancel/sign. |
| 13 | Existing `#157` / `#153` leftover tests remain PASS. |
| 14 | **Slow HOT envelope:** a HOT collect that would run past 25s leftover-stops / aborts so coordinator return is ≤ ~30s (25s + 5s grace). A second HOT is not started while the first is in progress. After return, the next due slot is used (missed slot not queued). |
| 15 | **T+3h unknown expiry (#164):** kickoff-passed + `in_running None` at T+2h59m is HOT (not live). At T+3h01m it **leaves current radar** (DROP from Discovery/Tracked/HOT identity), still `in_running is None`, `fixture_status` not rewritten to completed. |
| 16 | **Startup bootstrap:** new coordinator / empty process-memory store → Tracked `[]`; `plan_tick(now=start)` returns **UNIVERSE** (does not wait 180s and does not run an empty HOT cycle first). After the first bootstrap chunk records inventory, Tracked may become non-empty; HOT membership is classified from that inventory. |
| 17 | **#161 identity seam:** after a HOT upsert of a subset, `FixtureCurrentStateStore.resolve_canonical_id` still maps cluster id, source event id, and paper-decision event id to the same fixture; `test_tracked_fixture_click_through.py` stays PASS. |
| 18 | **Freshest status vs HOT economics:** an older HOT `in_running=True` snapshot must not pin membership/detail after a later UNIVERSE observation with `in_running=None` beyond the 3h window. Classify from the freshest provider-status observation; Tracked HOT membership still uses HOT economics only. |
| 19 | **Failed UNIVERSE chunks do not consume successful-work budget:** a provider timeout/exception keeps cursor/evaluated IDs, applies bounded backoff, and does not close the generation. HOT still starts on its due slot. Successful-work budget exhaustion with leftovers **pauses** the same generation until the next continuation window. |
| 20 | **One collection owner:** when `server_loop_enabled`, frontend auto-refresh polls `GET /paper/live-refresh` and does not `POST /paper/collect`. |
| 21 | **Manual collect isolation:** primary `POST /paper/collect/hot` and Advanced `POST /paper/collect` do not increment/reset `generation_work_used_s`, universe cursor, or HOT/UNIVERSE due times. If a scheduled lane is active, either fails fast (409). |
| 22 | **Live paper auto-capture:** a qualifying `LIVE_PAPER` decision with `paper_autofill_enabled` opens once through `persist_triggered_chain`; repeated HOT observations are idempotent; allocator rejection / stale quote / Tracked-Near do not open; treasury/journal provenance is `live_paper`. |
| 23 | **Demo isolation:** labelled `/demo` fixture replay passes `autofill=False` and does not inherit the global live auto-capture flag. |

Frontend: health-bar / scan-note tests that Fast scan and Full sweep both render; a single `Last scan` string is insufficient once the API nests lanes.

Do not use live Windows as the first proof of classification; clocked unit tests first. Owner-Windows smoke is **after** the separate implementation child PR, not this design PR.

## 13. Implementation sequence (separate child PR)

1. ~~Land #157 / #118 timeout correction~~ **Done** — owner-Windows PASS; merged into #131 `3de14fc6`.
2. ~~#160/#161 identity/current-state store~~ **Done** — architect PASS; merged into #131 `292e8109`. Dual cadence **extends** this store.
3. Slice 1 + store upsert: classifier (3h bound) + lane upsert/TTL merge on existing `FixtureCurrentStateStore` + Tracked merge tests (fake reports, no HTTP).
4. Slice 3–5: coordinator dual loop — HOT 25s/30s envelope, UNIVERSE chunks, preemption+cursor, startup bootstrap.
5. Slice 6–9: API/UI honesty (Fast vs Full) without breaking #161 click-through.
6. Exact-head CI: backend pytest, Ruff F, frontend tests/typecheck/build.
7. Implement as a **separate draft child** of this accepted design + current #131. Stop for architect review before merge to `main`.

## 14. Out of scope

- Live execution, new venues, betting strategy, settlement redesign, treasury redesign.
- Changing `max_event_pairs` as a substitute for segmentation.
- Extending HOT to minutes, or giving HOT the 45s explicit-collect budget.
- A single 150s auto-loop `collect_and_scan`.
- Rewriting or importing the dislocation burst engine.
- Persistent fixture-inventory SQLite.
- Giving explicit `POST /paper/collect` the 150s Full Sweep generation budget.
- Research surfaces.
- Reintroducing browser-driven scans to implement paper auto-capture. Qualifying `LIVE_PAPER` auto-capture uses `persist_triggered_chain()` inherit on the server-owned collector persist path only. `/demo` fixture replay stays explicit/manual (`autofill=False`).

## 14a. Paper auto-capture (folded into #162)

Owner product: genuinely qualifying live paper opportunities are automatically paper-traded, not merely displayed.

- Inherit `settings.paper_autofill_enabled` only when provenance is `LIVE_PAPER`. Explicit `autofill=False` always wins; explicit `True` remains a test/operator override.
- Windows launcher may set `PAPER_AUTOFILL_ENABLED=true` for that local process. Application default stays false. `SPORTS_HEDGE_MODE=paper` and `SPORTS_HEDGE_EXECUTION_ENABLED=false` remain mandatory.
- Gates are unchanged: canonical equivalence, solver arbitrage, fees/FX, executable depth/liquidity/risk, quote freshness, allocator-accepted positive sized plan. Tracked/Near or a gross price must not open a trade.
- Size comes from the allocator plan. Do not hard-code £10. Capital/FX/depth/risk blocks fail closed with an auditable `_entry_rejections` reason.
- Repeated HOT observations of the same still-open opportunity do not duplicate OPEN trades, treasury locks, or realized P&L (`simulate_fill` existing-trade short-circuit).
- No startup backfill of prior watchlist/discovery rows (including Leeds v Newcastle 1.35% net). First completed live paper trade must be a fresh qualifying observation after this fix.
- UI: `AUTO PAPER CAPTURE ON` plus retained `PAPER MODE · NO EXECUTION`.

## 15. Architect decisions (accepted, review `5196716600`)

| # | Decision |
| --- | --- |
| 1 | Wave G correction: explicit `POST /paper/collect` is a bounded **20s manual diagnostic** (+5s coordinator grace), separate from scheduled Fast/Full. |
| 2 | UNIVERSE **generation** budget is **150s**, not 180s. Keep 30s headroom inside the **180s** sweep cadence. 150s is executed as **resumable chunks**, not one job. |
| 3 | Radar TTLs: **HOT 90s / UNIVERSE 360s**. Executable quote freshness remains the existing fail-closed ~1s contract. |
| 4 | Kickoff-passed + unknown in-play: HOT without fabricating provider `in_running`, only within **3h**, unless a successful current evaluation still shows matched equivalents (operator IN PLAY) or confirms zero equivalents (leave current HOT radar, no fabricated completed). After 3h, expire from **current radar** (#164) unless the provider explicitly says in-running or postponed/delayed/rescheduled. Do not claim completed from time. |
| 5 | Canonical fixture current-state store: **process memory for v1**. Restart honesty: Tracked empty until collection. **Immediate UNIVERSE bootstrap** on startup. SQLite inventory later. |
| 6 | HOT ordering: **simple deterministic key** (§5.5). Do not couple the dislocation burst scheduler. |
| A | Separate HOT cycle timeout default **25s** so the 30s cadence is physically achievable. #157 4s reserve + 5s grace ⇒ ~30s envelope. HOT must not overlap itself. |
| B | Each UNIVERSE scheduler run processes a chunk only until `next_hot_due - safety_margin`, persists cursor, yields, resumes. Acceptance must prove forward progress across multiple HOT cycles without starving HOT. |

No remaining open product questions for v1. This PR stays docs-only. Implementation is a separate draft child of current #131 `292e8109`.

## 16. Tenet review (this design pass)

**Applicable:** 02, 03, 04, 09, 11, 12, 14, 15.

**Satisfied by the plan (not yet by code):**

- 02 — paper-only; no execution path.
- 03 — one canonical identity; no lane-specific IDs.
- 04 — near ≠ triggered; qualifying from either lane; expired fail closed.
- 11 — Fast vs Full; radar_current ≠ executable; empty until first collection; unknown in-play not labelled live; no fabricated completed.
- 14 — HOT is the bounded priority cadence that can actually meet 30s; no courtsiding; burst scheduler not silently reused.
- 15 — both lanes use existing cost/FX fail-closed.
- 12 — this review.

**Partial / deferred:** implementation (separate child PR); burst overlay; SQLite inventory.

**Conflicts:** none accepted. The Tracked latest-cohort contract **must** change; that is documented, not silent. HOT 45s-on-30s-cadence would have been a silent cadence lie; 25s timeout is the correction.

**Data class of this PR:** design/docs only. No live, historical, modelled, or fixture data is shown by this change.

**Safety:** no venue write/place/cancel/sign. `execution_enabled=false`.
