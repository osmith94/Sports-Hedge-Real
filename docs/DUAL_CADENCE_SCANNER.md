# Dual-cadence scanner — architecture and implementation plan

**Issue:** #158
**Status:** Design only. Stop for architect review. Do not implement, do not merge to `main`, do not modify #131, do not land commits on the #118 child until that timeout correction is accepted.
**Date:** 14 September 2026

This is a scanner/scheduler/read-model change. It does not add venue write, place, cancel, or sign paths. Phase 1 remains `SPORTS_HEDGE_MODE=paper` / `SPORTS_HEDGE_EXECUTION_ENABLED=false`.

## 1. Implementation base and stacking

Inspected 14 September 2026:

| Object | Ref | Role |
| --- | --- | --- |
| #131 | `cursor/paper-demo-consolidation-08fc` @ `6fc68e97bb248ea0392f569ea54764464a23b868` | Paper demo consolidation. **Do not modify.** |
| #157 (Issue #118 child) | `cursor/scan-soft-budget-finalisation-afe6` @ `51fbd24034654bb9e05ca413c8707f1d9d4843ac` | Partial live-scan finalisation before the 50s hard timeout. **Proposed implementation base.** |
| This document | stacked *on* #157 conceptually; this PR is docs-only | Dual-cadence plan |

#157 is available and already contains #131 head `6fc68e97`. It is the correct base:

- 45s collector soft budget, 4s leftover-assembly reserve, 5s coordinator grace.
- Provider waits bound to remaining soft budget, not the 50s hard deadline.
- Partial `CollectionReport` with `#153` leftovers (`not_evaluated_scan_deadline`; Equivalent 0 only for actually evaluated fixtures).
- `cycle_in_progress=false` after a truthful partial report; HTTP 200 rather than empty 504.

**Do not race #157.** Dual-cadence work must reuse that leftover/budget machinery, not rewrite it. If #157 is superseded by a later accepted #118 child, rebase the implementation onto that child. Until architect review, implementation stays unstarted.

Owner-Windows credentialed re-smoke of #157 remains a gate for the timeout lane. Dual cadence must not change that 45s/50s contract for a single explicit `POST /paper/collect` until review says otherwise (see §8).

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
| 11 UI / data honesty | Distinct Fast scan vs Full sweep. Stale rows must not look current. Empty stays empty. |
| 12 Agent review | This document + PR template. |
| 14 Event-driven dislocation | Fast lane is the Phase 1 realisation of “increase snapshot frequency for affected/urgent events within rate limits.” Burst scheduler stays a later overlay, not this PR. |
| 15 Fees / FX | Both lanes run the same fail-closed economics. No invented costs. |

Non-applicable for this slice: 05–08, 10, 13, 16–18 except that paper-entry quote-age fail-closed (18/04) must not be weakened so distant Tracked rows can look live.

**Known conflict if mishandled:** keeping `Tracked = last_report.paper_decisions` after HOT-only cycles would hide distant fixtures *or*, if last_report is unioned carelessly, show expired HOT quotes as current. The contract must change explicitly (§6). Silently mixing stale rows is a Tenet 11 violation.

## 4. Current architecture (as of #157)

### 4.1 One loop, one lock, one report

`LiveRefreshCoordinator` (`backend/src/sports_hedge/application/live_refresh.py`):

- Single `interval_seconds` (default 30, settings `paper_live_refresh_interval_seconds`, clamp 15–300).
- Single `asyncio.Lock` around `run_cycle()`.
- Single `_last_report: CollectionReport`. `record_report()` **replaces** fixtures, paper decisions, venue health, and operator summary.
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

Cluster order today is `venue_count` descending (`fixture_clusters.py`). There is **no** kickoff / in-play / opportunity ranking in the live collector. The dislocation burst scheduler (`arbitrage/dislocations/scheduler.py`) is a separate ranking utility and is not wired into `collect_and_scan`.

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

Fixture drill-down (`fixture_detail`) reads **only** `_last_report`. A HOT-only last_report would hide UNIVERSE fixtures from the board and from drill-down.

### 4.5 Why the single cycle fails the product

On owner-Windows, a 60-fixture universe can consume the entire 45s budget (#118 / #157). Distant weekend cards and the 15:00 in-play window compete equally. Tenet 14 requires prioritisation under high-liquidity concurrency. The current seam for that prioritisation is missing in the live collector.

## 5. Target architecture

```text
                    ┌─────────────────────────────────────────┐
                    │     LiveRefreshCoordinator (one)        │
                    │  HOT due?  ──preempt──►  pause UNIVERSE │
                    │  UNIVERSE due?  (never starve HOT)      │
                    └────────────┬──────────────┬─────────────┘
                                 │              │
                    Lane A HOT   │              │  Lane B UNIVERSE
                    30s cadence  │              │  180s cadence
                    known IDs    │              │  discovery + sweep
                                 ▼              ▼
                    ReadOnlyCrossVenueCollector.collect_and_scan
                    (scan_lane, identity_scope, cycle_timeout,
                     leftover/budget rules from #157 unchanged)
                                 │
                                 ▼
                    CanonicalFixtureState (one store)
                    key = canonical_event_id / canonical_market_id
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
        Tracked board      Near / Triggered    Operator status
        current-state      executable          Fast scan ≠ Full sweep
        merge + TTL        freshness 1s        + per-fixture last_scanned
```

### 5.1 Lane A — HOT / fast loop

| Parameter | Initial default |
| --- | --- |
| Cadence | 30 seconds (`paper_live_refresh_hot_interval_seconds`) |
| Soft budget | 45 seconds (reuse `paper_scan_cycle_timeout_seconds` + #157 4s reserve + 5s grace) |
| Cohort | (a) fixtures with **truthful** provider in-play (`in_running is True` from Matchbook state), plus (b) fixtures with `kickoff_utc` in `(now, now + 60 minutes]` |
| In-play labelling | If provider in-play truth is unavailable, kickoff proximity may keep the fixture in HOT, but `in_running` / live-score fields stay unset/false. Do not label it live. |
| Identity source | Canonical store (already-known IDs). Prefer `list_markets` / books for those events. Do **not** rediscover the whole world every 30s. |
| Priority inside the lane | 1. truthful in-play 2. nearest kickoff 3. strongest current opportunity state (TRIGGERED / NEAR / unmatched last) |
| Output | Refresh executable market/book economics needed for qualification: prices, depth, fees/FX/risk/survivability as already modelled. Bound and return partial truthful state (#157). |
| Radar TTL | 90s (2× cadence + 30s slack). After TTL with no HOT refresh, the fixture is no longer HOT-current. |

HOT may include a fixture that UNIVERSE has not fully evaluated yet if kickoff/in-play membership is known from inventory.

### 5.2 Lane B — UNIVERSE / full sweep

| Parameter | Initial default |
| --- | --- |
| Cadence | 180 seconds (`paper_live_refresh_universe_interval_seconds`) |
| Soft budget | 150 seconds (new `paper_scan_universe_cycle_timeout_seconds`, ge 45, le 180). Still leftover-safe with #157 reserve/grace. Architect may pick 120–180; must stay bounded. |
| Cohort | Full currently captured/in-scope universe, including T+6d. |
| Purpose | Discover new fixtures/markets; keep distant fixtures on radar; detect initial cross-venue mispricing; promote into HOT as kickoff approaches. |
| Progress | Resumable cursor (`universe_cursor_canonical_event_id` + generation). Incomplete sweep retains evaluated work and marks the remainder `not_evaluated_scan_deadline`. Next sweep continues; it does not discard completed work from this generation. |
| Preemption | Between clusters (and at leftover assembly start), if HOT is due or running, UNIVERSE **yields**. Partial report is recorded as a UNIVERSE generation progress snapshot, not as a replacement of HOT state. |
| Radar TTL | 360s (2× cadence). Distant fixtures remain current-state until TTL or a newer valid observation. |

UNIVERSE must never starve HOT. If a sweep cannot finish inside one budget, that is expected: leftover + resume.

### 5.3 Shared-state rules

1. **One canonical identity store.** Both lanes upsert the same `canonical_event_id` / `canonical_market_id` (existing `canonical_source_event_id` / `canonical_matched_market_id` / `watch:{canonical_market_id}`). No second ID system, no fuzzy join between lanes.
2. **HOT preempts UNIVERSE.** UNIVERSE must not monopolise provider concurrency, collector inflight tasks, or coordinator state when HOT is due.
3. **Avoid duplicate calls.** UNIVERSE discovery maintains inventory. HOT refreshes known hot IDs. HOT may do a tiny identity repair (`list_events` for a missing kickoff/in-play flag) but not full pagination.
4. **Promote / demote automatically.** A future fixture becomes HOT when `now >= kickoff - 60m` or truthful `in_running`. After completion/expiry (provider status, or kickoff + configured post-match horizon using existing fixture_status rules), it leaves HOT. Do not invent “completed” from missing data.
5. **Qualifying arbs from either lane surface immediately.** Persist watchlist observations during the producing cycle, same as today. Do not buffer UNIVERSE TRIGGERED until the next HOT tick.
6. **Preserve** settlement equivalence, fees/FX/depth/risk fail-closed, paper-only venues, append-only audit.

### 5.4 Promotion / demotion function

Pure, clock-injected, deterministic:

```text
classify_scan_lane(fixture, now, *, hot_horizon=60m) -> HOT | UNIVERSE | DROP

DROP when provider status is completed/settled/void/expired (explicit only)
HOT  when in_running is True
     or (kickoff_utc - now) in (0, hot_horizon]
     or (in_running is not True and kickoff_utc <= now and not DROP)
        # kickoff-passed, in-play unknown: keep HOT by proximity, do not label live
UNIVERSE otherwise (including T-6d, T-4h)
```

`in_running is True` is the only live label. Kickoff-passed with `in_running is None` is HOT-by-proximity, `in_running` remains null on the read model.

## 6. Tracked contract change (required)

### 6.1 What must change

The current rule **Tracked = paper decisions of the single latest completed `CollectionReport`** cannot survive two cadences.

If HOT completes with five in-play markets, today’s API would show only those five and drop every T-6d row. If we naively unioned the last HOT report with the last UNIVERSE report, a stale HOT quote from a finished match could sit next to a fresh UNIVERSE row with no way to tell which is current.

### 6.2 Replacement: current-state merge by identity

**Tracked is the current radar board**, not “whatever the last HTTP cycle happened to evaluate.”

For each `canonical_market_id` (opportunity id `watch:{id}`):

| Lane of the latest **valid** observation | Membership rule |
| --- | --- |
| HOT | Show the latest HOT observation while `now < last_scanned_at + hot_ttl` (default 90s). If TTL expires with no refresh, drop from Tracked (fail closed). History/activity remain. |
| UNIVERSE | Show the latest valid UNIVERSE observation while `now < last_scanned_at + universe_ttl` (default 360s) **and** the fixture is not currently HOT. When the fixture is HOT, HOT observation wins; do not display a older UNIVERSE book as if it were the live quote. |
| Either lane, TRIGGERED / qualifying | Persist immediately. Tracked includes it if the radar TTL for **that observation’s lane** still holds. `/near` and `/triggered` continue to apply **executable** quote-age (`max_quote_age_ms`, default 1000) so a 90s-old UNIVERSE TRIGGERED does **not** remain an executable arb. It can remain on Tracked as radar with `freshness_class` honest. |
| Leftover `not_evaluated` this pass | Does **not** clobber a previous valid evaluated observation for that identity. Diagnostics record unevaluated-this-pass. Equivalent 0 remains reserved for actually evaluated empty sets (#153). |
| Expired / completed fixture | Leave Tracked. Do not carry last odds forward as current. |
| No completed collection yet | Tracked stays `[]` (unchanged honesty). |

**Do not** treat an empty HOT cohort as “empty Tracked.” An empty HOT cycle with a live UNIVERSE generation still shows distant current-state rows.

**Do not** keep a market on Tracked merely because SQLite history exists (today’s activity-vs-board split stays).

### 6.3 Freshness classes (operator-visible, compact)

Per tracked row:

```text
scan_lane: hot | universe
last_scanned_at
next_due_at          # now + remaining cadence for that lane
freshness_class:
  executable         # passes existing max_quote_age_ms (live quote)
  radar_current      # inside lane radar TTL, not executable-fresh
  expired            # past TTL — must not be returned as current
```

`expired` rows are omitted from Tracked (fail closed), not shown as live with a small print footnote. Diagnostics/activity may still mention them.

Paper BET / 8F / priority alerts continue to require `executable`. Radar_current is watch-only. That is how distant fixtures stay on the board without looking like live arbs (Tenet 11).

### 6.4 Near / Triggered

Unchanged economically:

- TRIGGERED only when settlement, fees, FX, depth, risk, and **executable quote age** pass.
- A UNIVERSE qualifying arb is written in that cycle and appears on `/triggered` immediately; the next GET after 1s wall-clock ages it out of TRIGGERED exactly as HOT does today.
- Do not invent a 180s executable window for far-future arbs. Survivability/freshness stays fail-closed. “Surfaces immediately” means **no lane delay**, not **quotes stay live for the sweep interval**.

### 6.5 Degraded / partial sweeps

Replace `test_degraded_completed_refresh_shows_only_that_cohort`:

- A degraded UNIVERSE sweep that evaluated `{healthy}` and leftover `{rest}` **keeps** previous valid current-state for `{rest}` if still inside universe TTL, and shows `{healthy}` from this sweep.
- Venue health for that **lane** is degraded/timeout as today.
- Leftover fixtures are listed in that lane’s `not_evaluated_count` / fixture inventory as `not_evaluated_scan_deadline` without wiping prior economics.

A failed lane (no report at all, hard timeout after #157 is supposed to have returned partial) must not blank the other lane’s current-state.

### 6.6 Fixture inventory / drill-down

`discovered_fixtures` on `GET /paper/live-refresh` must become the **canonical inventory snapshot**, not the last cycle’s cluster list. Each row carries `scan_lane`, `last_scanned_at`, `market_evaluation_state`, `opportunity_state`. Drill-down (`fixture_detail`) reads the store by `canonical_event_id`.

HOT cycles upsert only the fixtures they touched. They must not delete UNIVERSE rows.

## 7. Code seams (narrow)

Implement in this order after approval. Do not start until architect review.

| Slice | Seam | Change |
| --- | --- | --- |
| 0 | `#157` leftover/budget | **No functional change.** Call with per-lane `cycle_timeout_seconds`. |
| 1 | New `application/scan_lanes.py` | Pure `classify_scan_lane`, HOT sort key, TTL helpers. Clock injected. |
| 2 | New `application/fixture_current_state.py` | In-process canonical store (coordinator-owned). Upsert by canonical ids. Merge rules §6. Optional SQLite later; Phase 1 process memory + existing watchlist SQLite is enough if restart honesty is “Tracked empty until a collection completes” (already true). |
| 3 | `LiveRefreshCoordinator` | Two lane schedules; `hot`/`universe` status objects; preemption event; shared provider gate; `last_report` becomes insufficient — keep it as **last completed lane report** for debug, but Tracked/inventory must not use it alone. |
| 4 | `collect_and_scan(..., scan_lane, identity_scope)` | `identity_scope=None` → current discovery path (UNIVERSE). `identity_scope=list[canonical_event_id]` → skip full `list_events` pagination; fetch markets/books for known source ids on the inventory. Reuse cluster scan + leftover. |
| 5 | `server_owned_refresh_tick` | Dual due-logic: if HOT due, run HOT; else if UNIVERSE due (and HOT not due), run UNIVERSE. Explicit `POST /paper/collect` stays UNIVERSE-shaped for #118 comparability (§8). |
| 6 | `GET /paper/watchlist/tracked` | Build cohort ids from current-state store, not `last_report.paper_decisions` only. |
| 7 | `WatchObservation` / `NearOpportunity` / `DiscoveredFixture` | Additive: `scan_lane`, `last_scanned_at` (alias of observation time), `next_due_at`, `freshness_class`. |
| 8 | `Settings` | See §9. Keep `paper_live_refresh_interval_seconds` as alias of HOT interval so old env files work. |
| 9 | Frontend | Fast scan / Full sweep copy on health bar + scan note. Compact. Diagnostics stay Advanced. |
| 10 | Docs | This file + ADR 0002 + DEMO_READINESS Tracked section. |

**Do not touch:** treasury, allocator, unwind, venue order contracts, Research, dislocation burst engine internals (optional later: HOT sort can call the existing ranking key; not required for v1).

Suggested HOT sort without pulling in burst models:

```text
(-1 if in_running is True else 0,
  kickoff_utc,                          # nearest first
 -opportunity_rank,                     # TRIGGERED > NEAR > matched > unmatched
  canonical_event_id)                   # stable
```

## 8. API / operator status

### 8.1 `GET /paper/live-refresh`

Additive nested objects. Keep legacy scalars as **HOT** aliases so old UI does not invent a blended timestamp.

```text
hot:
  cadence_seconds           # 30
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
  cadence_seconds           # 180
  … same shape …
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
Full sweep · 1m ago · 41s · next 2m · 104 universe · 60 evaluated / 44 not evaluated
```

Do not ship a single `Last scan` once both lanes exist.

### 8.2 `POST /paper/collect`

Keep the explicit operator/Windows collect as a **UNIVERSE** (full-scope) collect with the existing 45s timeout **until #118/#157 is accepted and architect agrees to give explicit collect the larger UNIVERSE budget**.

Rationale: owner-Windows smoke and #157 tests assert 45s/50s, 60 fixtures, leftover truth. Dual-cadence auto-loop is what changes cadence. Changing explicit collect in the same PR would race the timeout lane.

Optional later: `scan_lane=hot|universe` on the request model (default `universe` for POST).

### 8.3 Watchlist

`GET /paper/watchlist/tracked` semantics change as in §6. Response rows gain additive freshness fields. No new identity prefix.

## 9. Settings

| Setting | Default | Notes |
| --- | --- | --- |
| `paper_live_refresh_hot_interval_seconds` | 30 | ge 15, le 60 |
| `paper_live_refresh_universe_interval_seconds` | 180 | ge 60, le 300 |
| `paper_live_refresh_interval_seconds` | 30 | **Alias of HOT.** Keep for env/launcher compat. |
| `paper_hot_pre_kickoff_horizon_minutes` | 60 | |
| `paper_hot_current_state_ttl_seconds` | 90 | Radar TTL |
| `paper_universe_current_state_ttl_seconds` | 360 | Radar TTL |
| `paper_scan_cycle_timeout_seconds` | 45 | HOT + explicit collect (unchanged) |
| `paper_scan_universe_cycle_timeout_seconds` | 150 | Auto UNIVERSE only, after #157 accepted |
| venue/provider timeouts | 15 / 8 | Unchanged |

Windows launcher keeps `PAPER_LIVE_REFRESH_ENABLED=true`. No new execution flags.

## 10. Concurrency and rate-limit risks

| Risk | Why | Mitigation |
| --- | --- | --- |
| UNIVERSE holds `run_cycle` lock for 150s | Today one lock; HOT cannot start | Split lane execution: HOT has a high-priority gate. UNIVERSE checks `hot_due` **between clusters** and yields with a partial report. |
| Two `collect_and_scan` overlap | `_execute_collection` builds **new** HTTP clients per cycle; overlap doubles Matchbook/PM/K QPS | Shared `ProviderGate`: max one cluster-scan inflight per venue; HOT waits ≤ one cluster then preempts. Do not start UNIVERSE `list_events` while HOT is in `list_markets`/books if the gate is busy — HOT wins. |
| HOT rediscovers universe | 30s × full Gamma/Kalshi pagination | HOT `identity_scope` from inventory; UNIVERSE owns pagination. |
| UNIVERSE leftover wipes HOT books | `record_report` replaces `_last_report` | Stop using `_last_report` as Tracked source. Lane reports upsert the store. |
| SQLite watchlist writers | Two lanes persist decisions | Same repository as today; serialize persist on the coordinator (append-only, short). Do not hold the provider gate during persist. |
| `#157` cancel/uncooperative HTTP | Still required | UNIVERSE yield uses the same cancel + leftover assembly; do not block on `aclose()`. |
| Matchbook look-ahead 168h vs HOT 60m | Discovery already returns T+7d | UNIVERSE keeps them; HOT filters membership. Do not shrink `matchbook_fixture_lookahead_hours`. |
| Burst scheduler unused | Separate module | v1 does not require it. Optional later for in-HOT ranking. Do not run a third loop. |

Provider timeouts stay #157-bounded (`remaining soft budget`, `MIN_PROVIDER_WAIT_SECONDS`). HOT’s smaller cohort should usually finish well under 45s; if not, partial leftover still applies.

## 11. Migration / backward compatibility

| Surface | Compat |
| --- | --- |
| Env `PAPER_LIVE_REFRESH_INTERVAL_SECONDS` | Continues to set HOT cadence. |
| `LiveRefreshStatus.interval_seconds` / `last_completed_at` | Remain, documented as HOT aliases. UI must switch in the same PR as the scheduler or operators will misread UNIVERSE work as “the” scan. |
| `GET /paper/watchlist/tracked` | **Breaking semantics**, additive fields. Same path. Tests in `test_tracked_current_snapshot.py` must be rewritten to the merge/TTL rules, not deleted. |
| Watchlist SQLite | Additive columns or JSON sidecar on opportunity; old rows: `scan_lane=null` treated as `universe` with `last_seen_at` as `last_scanned_at`. Missing lane + age > universe TTL → omit from Tracked (fail closed). |
| `#157` hang/partial tests | Must stay green. Do not change leftover reason strings. |
| Explicit collect 45s/60 pairs | Unchanged until architect signs UNIVERSE budget on POST. |
| Restart | Process memory inventory empty → Tracked empty until a collection completes (same as today). Watchlist history remains. |

No data backfill job. No second canonical ID migration.

## 12. Deterministic acceptance tests

New module `backend/tests/test_dual_cadence_scheduler.py` (clock injected; no live HTTP). Fixtures at **T-6d, T-4h, T-59m, kickoff, in-play, completed**.

| # | Assertion |
| --- | --- |
| 1 | Classifier: T-6d and T-4h → UNIVERSE; T-59m → HOT; kickoff-passed + `in_running True` → HOT live; kickoff-passed + `in_running None` → HOT membership, `in_running` stays None; completed status → DROP. |
| 2 | T-59m and in-play refresh on HOT cadence without waiting for UNIVERSE (advance clock 30s; HOT ran; UNIVERSE did not). |
| 3 | T-6d is present on Tracked after UNIVERSE and is **not** in HOT identity_scope on the next HOT cycle (no `list_markets` for that id). |
| 4 | UNIVERSE in cluster_scan when HOT becomes due: UNIVERSE yields; HOT cycle starts before UNIVERSE budget elapses; HOT `last_completed_at` updates. |
| 5 | Far-future qualifying paper_decision from UNIVERSE appears on `/triggered` and Tracked in that same cycle (`freshness_class=executable` at `as_of=observed_at`). |
| 6 | Clock advance from T-61m to T-59m promotes the fixture into HOT membership automatically. |
| 7 | Partial UNIVERSE: evaluated ids keep economics; leftovers `not_evaluated_scan_deadline`; previous valid current-state for a leftover id is retained; `not_evaluated_count` explicit. |
| 8 | Same `canonical_event_id` / `canonical_market_id` across lanes; no duplicate Tracked rows. |
| 9 | `GET /paper/live-refresh` has distinct hot/universe status; Fast/Full fields independently true. |
| 10 | HOT TTL expiry drops a vanished in-play row from Tracked; activity history remains. UNIVERSE TTL expiry drops a T-6d row rather than showing it current. |
| 11 | Empty HOT cycle does not empty Tracked of in-TTL UNIVERSE rows. |
| 12 | `execution_enabled=false`; collector/venue modules still have no place/cancel/sign. |
| 13 | Existing `#157` / `#153` leftover tests remain PASS. |

Frontend: health-bar / scan-note tests that Fast scan and Full sweep both render; a single `Last scan` string is insufficient once the API nests lanes.

Do not use live Windows as the first proof of classification; clocked unit tests first. Owner-Windows smoke is **after** integration onto the accepted #118 child, not this design PR.

## 13. Implementation sequence (post-review)

1. Land #157 / #118 timeout correction (owner-Windows re-smoke). Do not combine with dual cadence.
2. Slice 1–2: classifier + current-state store + Tracked merge tests (can run against fake reports, no HTTP).
3. Slice 3–5: coordinator dual loop + HOT identity_scope collector path + preemption test.
4. Slice 6–9: API/UI honesty.
5. Exact-head CI: backend pytest, Ruff F, frontend tests/typecheck/build.
6. Integrate onto accepted #131/#157 line. Stop for architect review before merge to `main`.

## 14. Out of scope

- Live execution, new venues, betting strategy, settlement redesign, treasury redesign.
- Changing `max_event_pairs` as a substitute for segmentation.
- Extending HOT to minutes.
- Rewriting the dislocation burst engine.
- Persistent fixture-inventory SQLite (nice later; not required if restart = empty Tracked until collect).
- Research surfaces.

## 15. Architect decisions requested

1. Explicit `POST /paper/collect` stays 45s UNIVERSE-scope until #157 is accepted — agree?
2. Auto UNIVERSE soft budget 150s vs 180s?
3. Radar TTL 2× cadence (90s / 360s) vs 1× + slack?
4. Kickoff-passed + unknown in-play: HOT-by-proximity without live label (proposed) vs UNIVERSE-only until Matchbook says in-play?
5. Process-memory inventory vs immediate SQLite fixture store?
6. Should HOT sort use `arbitrage/dislocations/scheduler.py` in v1 or the simple key in §7?

## 16. Tenet review (this design pass)

**Applicable:** 02, 03, 04, 09, 11, 12, 14, 15.

**Satisfied by the plan (not yet by code):**

- 02 — paper-only; no execution path.
- 03 — one canonical identity; no lane-specific IDs.
- 04 — near ≠ triggered; qualifying from either lane; expired fail closed.
- 11 — Fast vs Full; radar_current ≠ executable; empty until first collection.
- 14 — HOT is the bounded priority cadence; no courtsiding.
- 15 — both lanes use existing cost/FX fail-closed.
- 12 — this review.

**Partial / deferred:** owner-Windows smoke after implementation; burst overlay; SQLite inventory.

**Conflicts:** none accepted. The Tracked latest-cohort contract **must** change; that is documented, not silent.

**Data class of this PR:** design/docs only. No live, historical, modelled, or fixture data is shown by this change.

**Safety:** no venue write/place/cancel/sign. `execution_enabled=false`.
