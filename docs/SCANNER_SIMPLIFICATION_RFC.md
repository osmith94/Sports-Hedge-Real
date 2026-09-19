# Scanner simplification RFC — durable catalogue, full-universe price engine, background controls

**Issue:** #337
**Phase:** 1 — architecture / RFC only
**Status:** Draft for architect review (revision after CHANGES REQUIRED). Do not implement Phases 2–6 in this PR.
**Date:** 19 September 2026
**Base:** current `owner-live` `4060ef151f3c7cb22806c8e83ee78abfb5714df4`  
  (`Owner-live: fix paper auto-capture handoff` — #336 / PR #338 merged)
**Prior RFC head reviewed:** `21e685b7398723f7e40b58c51a14ef5066f241e1` (against then-`owner-live` `45d68d2`)
**Mode:** PAPER MODE · EXECUTION DISABLED
**Data class:** architecture/design from current code, existing tenets, architect review on PR #339, and cited owner-live operator symptoms. Not live quotes. Not modelled probabilities. Not fixture/demo UI data.

This document does **not** change production runtime behavior.

> **Product rule this program exists to satisfy:**
>
> UNIVERSE maintains a durable approved-market catalogue.
> One **price engine** eventually evaluates every ACTIVE catalogue row.
> HOT is a **priority tier** inside that engine, not exclusive membership.
> Audit, history, UI aggregation, checkpointing and diagnostics consume
> events **behind** that path. Fail-closed safety is not optional.

Related live contracts this RFC must preserve, not reopen:

| Contract | Issue / PR | Authority |
| --- | --- | --- |
| Paper / read-only venue boundary | Tenet 02 | `SPORTS_HEDGE_MODE=paper`, `SPORTS_HEDGE_EXECUTION_ENABLED=false` |
| Concurrent HOT + UNIVERSE workers | Tenet 19 | HOT must never cancel/reset UNIVERSE; no leftover-until-HOT time-slicing. Coverage guarantee: see §6.3 / §11 (formal update is a Phase 2 precondition). |
| Approved Match Register | #331 / Tenet 20 | Sole runtime market-equivalence authority for registered rows |
| UNIVERSE chunk watchdog + epoch quarantine | #330 | Bounded chunk, unbounded generation, stale callbacks quarantined |
| Compact off-loop UNIVERSE checkpoint | #334 / PR #335 | Resume telemetry only; ≤256 KiB; not on the event loop |
| Paper-eligible auto-capture outcome | #336 / merged PR #338 at `4060ef1` | Durable OPEN **or** explicit `PAPER_FILL_REJECTED`; do not duplicate or contradict |

---

## 0. Phase 1 acceptance answers

### 0.1 What is the minimum runtime needed to continuously find and paper-capture the four approved Matchbook/Kalshi families?

For each genuinely offered Matchbook↔Kalshi fixture, the operational runtime only needs:

```text
1. Discover in-scope fixtures (Matchbook events + Kalshi GAME/BTTS/TOTAL/FTTS series).
2. Deterministic canonical fixture identity (competition + canonical teams + kickoff).
3. Find at most the four registered families on that fixture and store exact native IDs:
     MATCH_RESULT_FT
     BTTS_FT
     TOTAL_GOALS_FT:{safe_half_line}
     FTTS_FT
4. Capture compact Kalshi fee metadata (and Matchbook cost registry) as a
   cacheable economics reference — not quotes.
5. Derive one price-engine item per ACTIVE catalogue row
     (HOT priority or BACKGROUND/CATALOGUE priority).
6. Refresh those exact books (complete required outcome set).
7. Apply already-known fees/FX (fail closed if unknown/partial).
8. Solve and publish the paper decision immediately.
9. If AUTO PAPER CAPTURE is ON and the item is eligible: attempt capture immediately,
   persisting OPEN or an explicit rejection. Never silently vanish.
10. A positive/qualifying BACKGROUND decision promotes that row to HOT priority
    immediately, without waiting for kickoff/lifecycle.
```

Everything else — full-event market census, unsupported-family depth, cycle-end audit
fan-out, UI aggregation, compact UNIVERSE resume checkpoints, mapping-review stores —
is either onboarding/diagnostics or an asynchronous consumer.

Operator label “HOT” / Fast scan may remain. **Price-engine coverage** is every
ACTIVE catalogue row. **HOT priority** is the frequent-cadence subset
(in-play / near-kickoff / already-interesting / opportunity-promoted).

### 0.2 Which current operations can move behind the critical path?

Already off the collector envelope, or can be:

- append-only paper-scan audit and cycle history;
- watchlist/activity fan-out that is not the capture gate;
- operator `/paper/live-refresh` aggregation (must stay a snapshot read);
- `/health` and `/build-info` (already cheap after #334);
- compact UNIVERSE checkpoint persistence (`asyncio.to_thread`, 20-fixture debounce);
- mapping census / exception-review / learned-label stores;
- Polymarket inventory on the Matchbook↔Kalshi paper path;
- Get Series / Get Market settlement re-proof of **registered** rows.

Must **not** move behind the path: register structural admission, exact-ID book
refresh of **every ACTIVE row**, known fees/FX (including Kalshi
`fee_type` / `fee_multiplier` resolution), freshness, allocator/treasury gates,
paper-entry idempotency.

### 0.3 Which current operations can the price engine stop doing once exact IDs are catalogued?

The HOT targeted-refresh path already skips `list_events` and `list_markets` /
`get_series` rediscovery when `hot_market_relationships` are present
(`collector.py`; Issue #320/#318). After a durable catalogue exists, the price
engine (HOT **and** BACKGROUND tiers) can also stop:

- depending on process-memory `FixtureCurrentStateStore` as the only ID source;
- re-running greedy market pairing / `MarketMatcher` as a discovery step;
- treating a missing relationship as a reason to list the whole event;
- sharing one cycle `remaining_soft` across unrelated fixtures so one
  `order_book_timeout after 8s` leftover-marks the rest of the roster;
- waiting for a whole collector batch to finish before publishing a decision;
- Get Series / Get Market **settlement re-proof** of registered rows (fee
  metadata is a separate compact snapshot — §9).

It must still re-validate that persisted IDs still resolve (gone / identity-changed
→ `hot_revalidation_needed`) and must still fail closed on stale books and
unknown/partial Kalshi fee metadata.

It must **not** stop pricing rows that sit outside the HOT lifecycle horizon.
Those rows are BACKGROUND-priority price-engine work, not “radar only until
kickoff.”

### 0.4 How should one slow Kalshi/Matchbook call affect only one work item?

Give each **price-engine item** (HOT or BACKGROUND) its own provider timeout and
process-memory retry state.

A Kalshi `order_book` that hits `paper_scan_provider_timeout_seconds` (default **8s**)
must:

- fail that work item (`order_book_timeout after 8s`, truthful, retryable);
- release that call’s claim on a Kalshi slot when the lease ends;
- leave every other queued item — including BACKGROUND items — eligible;
- **not** drive collector `remaining_soft` to 0 for the rest of the roster;
- **not** leftover-mark unrelated fixtures as `scan_budget_exhausted`.

A canonical item that needs several Kalshi contracts (H/D/A, FTTS) is still
**one item**. One missing/timed-out constituent fails that item; siblings
continue. Do not hold multiple provider slots across venues as a hostage set.
If all 4 Kalshi slots are genuinely occupied, remaining items wait as
`provider_capacity_saturated` / `deferred`, not as scan-budget failure.

Do **not** raise the 8s provider timeout or the 15s discovery timeout as the
primary fix. Do **not** broadly raise Matchbook/Kalshi concurrency (defaults 4/4)
without provider-safety evidence.

### 0.5 Which state must be durable vs process-memory/cache?

| State | Today | Target |
| --- | --- | --- |
| Approved Match Register (archetype → canonical key + admission policy) | code constant `REGISTER_VERSION=v1` | unchanged; still code. **Policy is not copied into the catalogue.** |
| Exact native market/contract IDs | process-memory `HotMarketRelationship` | **durable catalogue** (new) — scheduler source of truth |
| Price-engine queue / in-flight / short retry | n/a (shared collector leftovers) | **process-memory**, derived from ACTIVE catalogue rows |
| UNIVERSE generation cursor / evaluated IDs / work units | compact SQLite checkpoint v2 | unchanged resume telemetry; **not** a price-engine checkpoint |
| Raw `discovery_snapshot` / full `CollectionReport` | in-memory only (#334) | stay in-memory |
| Kalshi fee metadata | observation `metadata.kalshi_fee` / Get Series | compact durable/cacheable **fee snapshot table**; fail closed if unknown/partial |
| Matchbook commission | account-fee SQLite registry | unchanged |
| FX snapshots | FX DB | unchanged |
| Paper OPEN / rejection | paper ledger + watchlist | durable at item completion (merged #336 contract) |
| Radar current-state / TTL merge | process-memory `FixtureCurrentStateStore` | process-memory projection |
| Provider backoff / in-flight leases | process-memory | stay process-memory; reset bounded item backoff on restart |

`next_retry_at` is **not** durable. Resetting the existing `(2, 5, 10)`s item
backoff after a process restart is safe: restart already drops in-flight HTTP;
the first retry is equivalent to a new process seeing the same timeout; the
shared `ProviderAccessLayer` cooldown is the live “venue is sick” control and
is already process-memory. Making retry timestamps durable would recreate a
second checkpoint problem (#334). Append-only history may record retries
behind the path; it is not scheduler authority.

### 0.6 How do we migrate without breaking #330 / #331 / #334 / #336?

Additive catalogue table + optional Kalshi fee-snapshot table. **No durable
work-queue table.** No rewrite of the register. No fattening of the UNIVERSE
checkpoint. No second matcher. No persisted copy of paper-admission /
live-execution policy. No change to paper-only venue contracts. Auto-capture
continues to call `persist_triggered_chain`; Phase 4 only changes **when** that
call runs (item completion vs cycle-end batch) and must use the merged #336
OPEN-or-`PAPER_FILL_REJECTED` contract.

Phase 2 runtime must not start until the Tenet 19 contract update in §11 is
accepted (or stacked as the first commit of that phase).

---

## 1. Why this RFC exists

Sports Hedge has already made the hard correctness progress:

- deterministic Matchbook↔Kalshi fixture identity;
- Approved Match Register as the only runtime market-equivalence authority (#331);
- paper-assumed admission for exactly four full-time families (#326/#329);
- HOT targeted refresh of persisted exact IDs (#320/#318);
- concurrent HOT and UNIVERSE workers (Tenet 19);
- bounded UNIVERSE chunks with epoch quarantine (#330);
- compact off-loop checkpoints so persistence cannot stall `/health` (#334).

The remaining live problem is **orchestration complexity**, not matching theory.

Owner-live symptoms cited by Issue #337 (operator evidence, not reproduced in
this RFC):

```text
UNIVERSE: 68 fixtures, only 9 evaluated, 56 leftover
          provider failure: list_markets_timeout after 8s

HOT:      26 fixtures, some cycles 0/26 evaluated
          provider failures: order_book_timeout
          remaining soft budget reaching 0

many fixture rows: scan_budget_exhausted
```

Those strings are produced by current code, not by a loose UI paraphrase:

- leftover reason `scan_budget_exhausted` — `collector.py` `SCAN_BUDGET_EXHAUSTED_REASON`
- issue text `{stage}_timeout after {Ns}` — `collector.py` `_record_timeout`
- configured market/book budget **8s** — `Settings.paper_scan_provider_timeout_seconds`
- configured discovery budget **15s** — `Settings.paper_scan_venue_timeout_seconds`
- HOT collector envelope **25s** + **5s** coordinator grace — `paper_scan_hot_cycle_timeout_seconds` + `SCAN_CYCLE_RETURN_GRACE_SECONDS`

One slow provider path can consume enough **shared cycle budget** that unrelated
fixtures never get evaluated. That is the failure mode this program removes.

This program is **not** a request to remove fail-closed safety controls.

---

## 2. Applicable product contract

Read before any later-phase implementation:

| Tenet / spec | Why it applies |
| --- | --- |
| 02 Paper mode | No venue write/place/cancel/sign. Auto-capture is `simulate_fill`. |
| 03 Canonical equivalence | Register canonical key + required parameters. No fuzzy settlement. |
| 04 Arbitrage operations | Near ≠ triggered. Stale/rejected states stay visible. |
| 09 Capital / priority | Capture still allocator-sized; no double lock. |
| 10 Accounting / FX | One ledger; native currencies; no invented FX. |
| 11 UI / data honesty | Lane/provider/item failure must stay truthful. Radar ≠ executable. |
| 12 Agent review | This RFC + later-phase PRs. |
| 14 Event-driven dislocation | HOT cadence exists to catch dislocations; coverage must not collapse to 0/N. |
| 15 Fees / FX | Known costs required before paper eligibility. |
| 18 Fill risk / freshness | `paper_entry_max_quote_age_ms` default 2000. Idempotent capture. |
| 19 Concurrent workers | HOT and UNIVERSE overlap. No leftover-until-HOT. This RFC **refines** UNIVERSE “evaluate” to catalogue completeness; **full-universe economics coverage** moves to a price engine in which HOT is a priority tier. Formal tenet wording is a Phase 2 precondition (§11). |
| 20 Approved catalogue | Four locked families only on the MB↔K paper path. No runtime confidence/review admission. |
| `docs/DUAL_CADENCE_SCANNER.md` | Historical lane/TTL/Tracked contract. Tenet 19 overrides leftover-until-HOT chunking. |
| `docs/PHASE1_COMMON_MARKET_CATALOGUE_CENSUS_V3.md` | Four-family paper target. |
| `matching/approved_register.py` | Runtime register. |

Non-applicable for this program’s critical path: Research (05–08, 13, 17),
external/manual Polymarket legs as a reason to keep Polymarket on the MB↔K
HOT queue (16 still applies if a future item has an external leg).

**Tenet 19 contract (not a silent comment):**
Architect review on PR #339 supports moving the *mechanism* of comparison out
of UNIVERSE **provided** the *coverage guarantee* is preserved: every ACTIVE
supported catalogue row is eventually priced. That is now the target in §6.
It is **not** yet a product-contract change. Phase 2 runtime must not start
until `docs/core-tenets/19_CONCURRENT_HOT_AND_UNIVERSE_SCANNING.md` is updated
with the wording in §11 (separate docs PR or first commit of Phase 2).

---

## 3. Current end-to-end Matchbook/Kalshi scan path

### 3.1 Current-path diagram

```text
                    LiveRefreshCoordinator
                    (no global scan lock)
              ┌─────────────┴──────────────┐
              │                            │
     HOT async worker               UNIVERSE async worker
     cadence 30s                    persistent generation
     collector 25s + 5s grace       chunk wall ≈ 150−2 = 148s + 5s grace
     no self-overlap                epoch quarantines stale chunks (#330)
              │                            │
              └─────────────┬──────────────┘
                            │
                 collect_and_scan(scan_lane, …)
                 ONE cycle envelope:
                   hard = started + cycle_budget
                   soft = hard − min(4s, 20%·budget)
                   remaining_soft shared by every fixture in the wave
                            │
        ┌───────────────────┼────────────────────┐
        │                   │                    │
   skip list_events    list_events MB ∥     reuse discovery
   if identity_scope   Kalshi series        snapshot after
   + known events      (15s discovery       first chunk
                       budget; 1.5s post-
                       discovery reserve)
                            │
                   normalize + cluster
                   (max_event_pairs default 60;
                    multi-venue clusters are not dropped)
                            │
              cluster_sema 8  +  provider slots MB 4 / K 4 / PM 8
              ProviderAccessLayer: HOT priority, starve after 8 grants
                            │
        ┌───────────────────┴────────────────────┐
        │                                        │
 UNIVERSE per cluster                     HOT per relationship
 list_markets (MB)                        get_market(MB exact ID)
 nested/list_markets (Kalshi)             order_book(Kalshi exact ticker)
 Get Market/Series for candidates         no list_markets / get_series
 filter → Approved Match Register
 Kalshi order_book only if scan_eligible
        │                                        │
        └───────────────────┬────────────────────┘
                            │
                   PaperScanService.scan_pair
                   (register → fees/FX → depth → solver → allocation)
                            │
                   upsert FixtureCurrentStateStore  (process memory)
                   stream UNIVERSE fixture progress
                   dirty compact checkpoint (#334, off-loop, ≤256 KiB)
                            │
                   AFTER scan envelope:
                   audit + watchlist + persist_triggered_chain (auto-capture)
                   UI reads public_status() snapshot
```

### 3.2 Timeout / budget ownership (measured from `4060ef1`)

All defaults from `backend/src/sports_hedge/config.py` unless noted.

| Knob | Default | Bounds | Owner |
| --- | --- | --- | --- |
| HOT cadence | 30s | 15–60 | `paper_live_refresh_hot_interval_seconds` |
| HOT collector timeout | **25s** | 10–45 | `paper_scan_hot_cycle_timeout_seconds` |
| Coordinator return grace | **5s** | constant | `live_refresh.py` `SCAN_CYCLE_RETURN_GRACE_SECONDS` |
| HOT worst-case envelope | **~30s** | 25+5 | cadence physically achievable |
| UNIVERSE generation work budget | **150s** | 30–180 | `paper_scan_universe_generation_budget_seconds` |
| Chunk safety margin | **2s** | 0.5–10 | `paper_universe_hot_yield_safety_margin_seconds` |
| Scheduled UNIVERSE chunk wall | **148s** | `budget − margin`, floor 6s | `universe_chunk_wall_seconds` — `next_hot_due` is **ignored** (Tenet 19) |
| UNIVERSE worker cooldown | 8s | 5–15 | after a **terminal** generation only |
| Manual diagnostic collect | 20s + 5s grace | 10–45 | `POST /paper/collect` |
| Fallback collector timeout | 45s | 10–180 | not used by scheduled HOT/UNIVERSE plans |
| Discovery (`list_events`) | **15s** | 3–60 | `paper_scan_venue_timeout_seconds` |
| Markets / books / get_* | **8s** | 2–30 | `paper_scan_provider_timeout_seconds` |
| HTTP connect/read/write/pool | 5 / 8 / 8 / 5 | constant | `venues/base.py` `market_data_http_timeout()` |
| Finalisation reserve | min(4s, 20% of budget) | constant | `SCAN_FINALISATION_RESERVE_SECONDS` |
| Min provider wait | 0.05s | constant | `MIN_PROVIDER_WAIT_SECONDS` — remaining_soft below this → no new waits |
| Post-discovery soft reserve | 1.5s | constant | hung `list_events` must not leftover the whole universe |
| Cluster concurrency | 8 | 1–32 | fixture-level, not market-item-level |
| Matchbook / Kalshi / Polymarket slots | 4 / 4 / 8 | per venue | `ProviderAccessLayer` |
| HOT starvation grants | 8 | 1–64 | then UNIVERSE gets a slot |
| UNIVERSE provider backoff | (2, 5, 10, 20, 30)s | constant | cycle-level provider failure |
| UNIVERSE work retry backoff | (2, 5, 10)s | constant | work-unit / rehydration |
| Work max attempts | 3 | 1–8 | transients stay `RETRY_WAIT`; not auto-FINAL_FAILED |
| Checkpoint flush | every 20 fixtures or boundary | constant | off-loop |
| Checkpoint cap | **256 KiB** | constant | fat `report` / `discovery_snapshot` keys fail closed |
| Paper-entry freshness | 2000 ms | 250–10000 | `paper_entry_max_quote_age_ms` |
| HOT / UNIVERSE radar TTL | 90s / 360s | — | Tracked radar, **not** executable freshness |

Soft vs hard deadline inside one `collect_and_scan`:

```text
cycle_budget  = HOT 25s  |  UNIVERSE chunk ~148s
hard          = started + cycle_budget
soft          = hard − min(4s, 20% · cycle_budget)
remaining_soft shared by discovery + every in-flight cluster
```

When `remaining_soft < 0.05s`, `_provider_budget_exhausted()` is true. The
collector stops launching work. Unevaluated clusters become leftovers with:

```text
market_evaluation_state = not_evaluated_scan_deadline
market_evaluation_reason = scan_budget_exhausted
```

Timeout issue text from `_record_timeout`:

```text
{stage}_timeout after {configured}s
# or, if the wait was shortened by remaining_soft:
{stage}_timeout after {waited}s (configured_timeout={configured}s, remaining_soft={…}s)
```

Stages include `list_events`, `list_markets`, `get_market`, `get_series`,
`order_book`. This is exactly the owner-live `list_markets_timeout after 8s` /
`order_book_timeout` evidence.

HTTP read is independently capped at 8s (`market_data_http_timeout`). Raising
only the application budget without changing HTTP read would not help a hung
socket; raising both is explicitly **not** the primary solution.

### 3.3 Step inventory (A–F)

Legend: **A** required before the next market can be evaluated · **B** can block
unrelated fixture work · **C** timeout/budget owner · **D** already
durable/cacheable · **E** HOT repeats UNIVERSE-proven work · **F** failure scope.

#### 1. Venue fixture discovery

- Matchbook `list_events` → `/edge/rest/events` football window.
- Kalshi `list_events` per configured `kalshi_series_tickers` (GAME/BTTS/TOTAL/FTTS per competition), `with_nested_markets=true`.
- HOT skips this when `identity_scope` + known source events exist.
- UNIVERSE reuses `discovery_snapshot` after the first successful chunk of a generation. Compact checkpoint does **not** persist that snapshot (#334), so process restart rediscovers.

| A | B | C | D | E | F |
| --- | --- | --- | --- | --- | --- |
| Yes for UNIVERSE first chunk | Hung venue capped by 15s and 1.5s post-discovery reserve; still delays cluster start | Discovery 15s under remaining_soft | In-generation snapshot only | HOT does not re-list | Per-venue/series; cycle continues |

#### 2. Fixture canonicalisation / matching

- Normalize → `EventMatcher` cluster. Unknown/youth/women/reserve fail closed via `facts/team_registry.py`.
- `max_event_pairs` default 60 never drops a **multi-venue** cluster, which is how an owner-live UNIVERSE can report 68 fixtures.

| A | B | C | D | E | F |
| --- | --- | --- | --- | --- | --- |
| Yes | Hard-deadline truncate can strand later fixtures this chunk | In-process; hard deadline only | Canonical IDs enter work-set / current-state | HOT re-clusters known payloads (cheap) | Partial clusters still scanned |

#### 3. Market discovery

UNIVERSE: Matchbook `list_markets(event_id)` (prices often embedded); Kalshi nested markets or `list_markets(event_ticker)`; Get Market / Get Series for catalogue candidates.

HOT: Matchbook `get_market(event_id, market_id)` only; Kalshi `get_order_book` per persisted ticker. Missing relationships → `hot_relationship_missing`, **not** a silent fallback to full listing.

| A | B | C | D | E | F |
| --- | --- | --- | --- | --- | --- |
| Yes for that fixture | **Yes — 8s timeout holds a provider slot and burns shared remaining_soft** | Provider 8s + HTTP read 8s | Kalshi get_market/get_series single-flight cache (process) | HOT skips list rediscovery | Per-call timeout; **soft budget leftover is roster-wide** |

This is the primary stall.

#### 4. Approved Match Register lookup (#331)

`matching/approved_register.py`: `VENUE_NATIVE_ARCHETYPES` maps Matchbook
`match_odds` / `both_teams_to_score` / `total_goals` / `first_team_to_score` and
Kalshi `GAME` / `BTTS` / `TOTAL` / `FTTS` onto:

```text
MATCH_RESULT_FT
BTTS_FT
TOTAL_GOALS_FT:{safe_half_line}    # parameterized; integer/quarter rejected
FTTS_FT
```

`registered_canonical_key` is the PAPER cross-venue decision. Runtime scanning
must not re-litigate settlement text, mapping confidence, or learned labels for
a registered row. Extra-time / penalties / to-qualify stay unregistered.
Incomplete outcomes, wrong period, and TOTAL line mismatch fail closed through
the structural gate.

| A | B | C | D | E | F |
| --- | --- | --- | --- | --- | --- |
| Yes for solver/paper | No I/O | None | Code constant | HOT re-checks `scan_eligible_pair`; fail → `hot_revalidation_needed` | Pair skip only |

#### 5. Order-book / exact quote refresh

UNIVERSE fetches Kalshi depth **only** after `scan_eligible_pair`. Matchbook
UNIVERSE path uses list-markets prices. HOT refreshes exact IDs every cycle.

| A | B | C | D | E | F |
| --- | --- | --- | --- | --- | --- |
| Yes for that pair’s economics | **Yes — same shared remaining_soft / 4 Kalshi slots** | Provider 8s | Not durable; fail closed (no cached prices as executable) | HOT must refresh quotes; that is its job | Per-ticker, but cycle leftover is broader |

#### 6. Costs + FX

`PaperScanService.scan_pair`: `VenueCostResolver`, Matchbook account-fee store,
Kalshi series fee metadata, `FxRateService.paper_snapshots`. Missing material
costs/FX fail closed. Live scheduled collect rejects client-supplied FX/costs.

| A | B | C | D | E | F |
| --- | --- | --- | --- | --- | --- |
| Yes before calling an item paper-eligible | No venue I/O on the happy path | None in the scan envelope | Fee SQLite + FX DB; HOT may reuse persisted Kalshi fee_snapshot | Re-resolve; may reuse snapshot | Per-decision rejection |

#### 7. Paper decision + auto-capture

Decisions are computed **inside** the cluster scan. Persistence and auto-capture
run **after** the scan envelope (`api/paper.py` `persist_scheduled_collection_report`
→ `persist_triggered_chain`).

Today that means: a qualifying item found at fixture 3 of 26 still waits for the
HOT cycle to finish (or timeout) before capture is attempted. Combined with
shared `remaining_soft`, many cycles never produce a decision at all.

#336 / PR #338 has **merged** at `4060ef1`. Paper-assumed eligible rows no
longer vanish between scan audit and persistence. This RFC must not fork a
third capture path. See §10.1.

| A | B | C | D | E | F |
| --- | --- | --- | --- | --- | --- |
| Decisions before capture of those markets | Cycle-end batch waits for unrelated fixtures | Outside collector timeout | Paper audit + ledger + watchlist | HOT can capture on refresh | Persist failure must not rewrite scan truth |

#### 8. Status, checkpoint, UI

- `FixtureCurrentStateStore`: process-memory dual-lane upsert. Exact IDs live here as `HotMarketRelationship`.
- UNIVERSE checkpoint v2: work units, cursor, evaluated IDs, event **counts**. Not books, not relationships.
- Restart: restored EVALUATED IDs are `_universe_needs_rehydration` until a successful evaluated upsert rebuilds HOT identity (#334 correction). Failed rehydration stays pending with 2/5/10s backoff.
- `GET /paper/live-refresh` reads `public_status()` + recent scan-cycle audit. `/health` must not open `paper_settings.sqlite` or rebuild runtime state.

| A | B | C | D | E | F |
| --- | --- | --- | --- | --- | --- |
| Streaming upsert enables mid-sweep HOT promotion | Checkpoint I/O is off-loop; **restart rehydration re-enters UNIVERSE eval** | Checkpoint not under soft deadline | Resume durable; relationships not durable | HOT TTL 90s vs UNIVERSE 360s | Provider cycle failure → backoff; generation not reset |

### 3.4 What HOT already does **not** repeat

When identity scope and relationships are present (`hot_market_relationships.py`):

- no `list_events`;
- no `list_markets` / `get_series` catalogue rediscovery;
- no settlement re-proof via Get Market;
- no fallback to UNIVERSE listing when a relationship is missing.

HOT still does: cluster known payloads, exact-ID quote refresh, fee/solver on
fresh quotes, identity revalidation, and — after restart — waits for UNIVERSE
rehydration because relationships are not durable.

---

## 4. Where unrelated fixture work stalls today

These are the blocking dependencies to remove from the **critical path**.
Safety gates listed in §5 are not in this list.

### 4.1 Shared scan budget (highest impact)

One `collect_and_scan` wave owns one `remaining_soft`. Every in-flight
`list_markets` / `order_book` / `get_market` draws from it.

Worked example consistent with owner-live HOT 0/26:

```text
HOT collector 25s, reserve min(4, 5) = 4s → ~21s of cluster work
Kalshi slots = 4, provider timeout = 8s
4 concurrent order_book_timeouts ≈ 8s of wall, but
cancelled/held leases + Matchbook get_market + leftover assembly
can drive remaining_soft to 0 before later fixtures start.
Result: 0 evaluated, 26 leftover, reason scan_budget_exhausted.
```

Worked example consistent with UNIVERSE 9/68:

```text
UNIVERSE chunk ~148s. Cluster concurrency 8.
A Matchbook list_markets_timeout after 8s is isolated as a fixture
failure **only if remaining_soft still allows other clusters to start**.
Once remaining_soft < 0.05s, the rest of the roster is leftover, not retried
as independent items. Completeness = deadline_leftover.
```

**Target:** remaining_soft must not be the admission ticket for unrelated items.
Item timeout ≠ roster timeout.

### 4.2 Provider lease hold-until-task

`ProviderLease.hold_until_task` keeps a Matchbook/Kalshi slot occupied after the
collector has timed out the wait, until the underlying HTTP call actually ends.
That is correct for rate-limit safety. It becomes a stall when the scheduler
assumes “timed out ⇒ slot free ⇒ start the next 18 fixtures.”

**Target:** keep lease honesty; shrink the **unit of work** so a held slot blocks
one item, not a 26-fixture HOT roster leftover pass.

### 4.3 Repeated discovery / classification

UNIVERSE still lists/classifies an event’s market inventory to find the four
families, including discarding `phase1_non_target_family` after fetch. That is
acceptable **once per catalogue generation** for a fixture. It is not acceptable
as the HOT path, and it is not acceptable to redo it every chunk because exact
IDs were only in process memory.

Restart rehydration currently **re-evaluates** restored EVALUATED IDs to rebuild
HOT relationships. That re-enters list_markets/register work under the same
shared budget.

**Target:** durable catalogue rows. Restart loads exact IDs. UNIVERSE only
re-lists when a row is missing, terminal, or invalidated.

### 4.4 UNIVERSE doing the price engine’s job

`collect_and_scan` is still a full paper scan for both lanes: books, fees, solver,
decisions. UNIVERSE therefore spends Kalshi order-book slots and soft budget on
economics during catalogue maintenance. Under current Tenet 19 wording that was
“evaluate while you are there.” Under owner-live load it prevents the catalogue
from being maintained.

**Target:** UNIVERSE persists exact IDs and structural metadata. It may record
“family present / not listed / disappeared.” It must not need a fresh Kalshi
book to mark a fixture catalogued. Full-universe economics coverage moves to
the price engine (§6), including BACKGROUND rows outside the HOT horizon.

### 4.5 Cycle-end persistence and auto-capture

Audit + `persist_triggered_chain` already run **after** the collector envelope
(good: they must not count as `scan_cycle_timeout`). They still wait for the
**batch**. Combined with 4.1, capture never runs because the batch never
evaluates the item.

#336 / PR #338 has **merged** at `4060ef1` and is the correctness of that
handoff (no silent vanish; OPEN or `PAPER_FILL_REJECTED`). This RFC’s Phase 4
is the **timing** of that handoff (item completion). Do not reimplement the
watchlist leftover / bind-snapshot fix.

### 4.6 Status aggregation / health

#334 already moved fat checkpoint serialization off the event loop and made
`/health` / `/build-info` cheap. **Do not** put exact books or current-state
rows back into the UNIVERSE checkpoint. **Do not** make `/paper/live-refresh`
rebuild collector state per poll.

Remaining risk: treating UNIVERSE `deadline_leftover` or HOT `0 evaluated` as a
global Matchbook/Kalshi outage (Tenet 19 §10 / §20). Item-level health must
become first-class in later phases.

### 4.7 Polymarket on the default MB↔K paper lanes

Defaults `paper_hot_venues` / `paper_universe_venues` still include Polymarket.
Polymarket has 8 provider slots and competes for the cluster semaphore. The
Phase-1 paper register pair is Matchbook↔Kalshi only. Polymarket may remain a
diagnostics/census venue; it must not consume price-engine slots for the four approved families.

This is a scheduling-scope statement, not a request to delete Polymarket adapters.

### 4.8 What is already isolated (do not “fix” again)

| Control | Why it stays |
| --- | --- |
| No global HOT/UNIVERSE exclusion lock | Tenet 19 |
| UNIVERSE chunk wall independent of `next_hot_due` | #330 / Tenet 19 |
| Epoch quarantine of stale chunk callbacks | #330 |
| Compact checkpoint, off-loop, 256 KiB cap | #334 |
| Kalshi depth skipped for unapproved markets | #295 |
| HOT targeted exact-ID refresh | #320 |
| Register as sole registered-row authority | #331 |
| Paper-eligible OPEN or `PAPER_FILL_REJECTED` | #336 merged at `4060ef1` |
| Cheap `/health` | #334 |

---

## 5. Safety-critical vs behind the critical path

### 5.1 Safety-critical (must remain on the path that can open paper)

These may **fail closed**. They may not be skipped to improve coverage.

1. Phase 1 paper / read-only venue contracts. No `place_order` / `cancel_order` / signing.
2. Deterministic fixture identity. Wrong-team / wrong-kickoff contradiction remains hard.
3. Approved Match Register structural match for the four keys. No confidence/review admission.
4. Exact native IDs actually used for the book refresh (no silent substitute market).
5. Fresh executable quotes. Stale/suspended/missing books are not liquidity.
6. Known venue fees + FX before eligibility. Unknown/partial Kalshi `fee_type` /
   `fee_multiplier` must not become zero. Complete required outcome-set books
   before calling the item evaluated.
7. Solver + allocator + treasury gates. Tracked/Near is not permission to OPEN.
8. Paper-entry idempotency / no double lock (`simulate_fill` existing-trade short-circuit; fill-attempt bind).
9. Truthful provider failure attribution (timeout ≠ outage ≠ deferred).
10. HOT/UNIVERSE concurrency invariants (#330): no leftover-until-HOT, no stale chunk mutation.

### 5.2 Behind the path (must not gate the next unrelated item)

1. Append-only scan audit / cycle history.
2. UI inventory aggregation and freshness-class rendering.
3. Compact UNIVERSE generation checkpoint.
4. Mapping-review / learned-rule / ChatGPT stores.
5. Full-event unsupported-market census counts.
6. Get Series / Get Market settlement **re-proof** of already-registered rows.
7. Polymarket listing on the MB↔K four-family path.
8. Restart-time rebuild of radar TTL cosmetics (ACTIVE catalogue rows are enough to reconstruct the price-engine queue).
9. Durable work-queue / `next_retry_at` checkpoints (must not exist).

---


## 6. Target conceptual model

Distinguish **price-engine coverage** from **HOT priority**. The circular
blind spot below is rejected:

```text
outside HOT horizon → not priced → cannot discover edge → cannot promote to HOT
```

Required coverage: every ACTIVE Matchbook↔Kalshi row for the four approved
families is eventually refreshed and evaluated, even if it has never shown an
edge.

```text
DISCOVER FIXTURES
      ↓
DETERMINISTIC FIXTURE MATCH
      ↓
APPROVED MATCH REGISTER
      ↓
STORE EXACT NATIVE MARKET/CONTRACT IDS     ← UNIVERSE (catalogue maintainer)
      + compact Kalshi fee snapshot (no quotes)
      ↓
PRICE ENGINE  (process-memory queue derived from ACTIVE catalogue rows)
      ├─ HOT priority        in-play / near-kickoff / already-interesting /
      │                      opportunity-promoted — frequent cadence (~30s)
      └─ BACKGROUND priority all other ACTIVE rows — slower bounded cadence
      ↓
REFRESH EXACT BOOKS (complete required outcome set)
      ↓
CALCULATE ECONOMICS (prepared fees/FX + fresh books; fail closed if unknown)
      ↓
PUBLISH DECISION IMMEDIATELY
      ↓
AUTO PAPER CAPTURE OR EXPLICIT REJECTION   ← merged #336 contract, per item
      ↓
qualifying BACKGROUND decision → promote row to HOT priority immediately

Audit / history / UI / checkpoint / diagnostics consume events behind this path.
```

### 6.1 Target-path diagram

```text
                         Sports Hedge backend (paper / read-only)
                                      │
              ┌───────────────────────┴────────────────────────┐
              │                                                │
     UNIVERSE catalogue worker                      PRICE ENGINE worker
     persistent generation                          one engine, two priority tiers
     chunk watchdog #330 preserved                  HOT ~30s / BACKGROUND slower
              │                                     no exclusive HOT-only membership
     list_events (MB ∥ Kalshi series)                         │
     canonical fixture match                        derive items from ACTIVE rows
     for each new/dirty fixture:                    (process memory; restart rebuilds)
        list_markets / nested four families                   │
        register structural key                     HOT items first, then BACKGROUND
        upsert durable catalogue row                bounded concurrency (unchanged caps)
        capture Kalshi fee snapshot (no quotes)               │
        invalidate disappeared/terminal             per item:
     do NOT require executable books                  claim at most one slot per in-flight
     do NOT wait for the price engine                   HTTP call (no cross-venue hostage)
              │                                       MB get_market(exact)
              │                                       K  order_book(exact) per constituent
              │                                       assemble complete outcome set
              │                                       fees/FX snapshot (fail closed)
              │                                       scan_pair
              │                                       emit decision immediately
              │                                       if eligible ∧ autofill:
              │                                         persist_triggered_chain
              │                                         OPEN or PAPER_FILL_REJECTED
              │                                       qualifying → HOT priority now
              └───────────────────────┬────────────────────────┘
                                      │
                    Shared ProviderAccessLayer
                    MB 4 / Kalshi 4
                    HOT-tier requests keep existing priority + anti-starve vs UNIVERSE
                    item timeout holds one lease, not the roster
                    4 busy Kalshi slots → provider_capacity_saturated, not scan_budget
                                      │
                    Durable approved-market catalogue (scheduler source of truth)
                    Compact Kalshi fee snapshot table (referenced, not quotes)
                    Compact UNIVERSE checkpoint (existing, unchanged role)
                    Paper ledger / watchlist / audit (consumers)
                    FixtureCurrentStateStore (radar projection)
```

### 6.2 Minimum runtime for the four families

Per fixture, UNIVERSE attempts **only**:

| Canonical key | Matchbook native | Kalshi native | Structural gate | Typical Kalshi constituents |
| --- | --- | --- | --- | --- |
| `MATCH_RESULT_FT` | Match Odds / Final Result | GAME | FULL_TIME + HOME/DRAW/AWAY | 3 YES contracts |
| `BTTS_FT` | Both Teams To Score | BTTS | FULL_TIME + YES/NO | 1–2 contracts |
| `TOTAL_GOALS_FT:{line}` | Total Goals | TOTAL | FULL_TIME + exact safe half-line + OVER/UNDER | OVER/UNDER pair |
| `FTTS_FT` | First Team To Score | FTTS | FULL_TIME + HOME/AWAY/NO_GOAL | 3 contracts |

If Kalshi does not list that family for the fixture: catalogue row
`NOT_LISTED` / `VENUE_UNAVAILABLE`. No depth, no solver, no price-engine item.

If Matchbook lists extras (DNB, handicap, player props): discard immediately.
Do not create a catalogue row. Do not fetch Kalshi books for them.

Lane classification (`classify_scan_lane`) still decides **HOT vs BACKGROUND
priority**, not whether the row is priced. A T-6d ACTIVE row **is**
background-priced. A qualifying background decision promotes it to HOT
priority immediately (Issue #200 opportunity promotion), without waiting for
kickoff or the 60-minute horizon.

### 6.3 Coverage guarantee vs current Tenet 19 wording

UNIVERSE still:

- discovers;
- matches fixtures;
- streams incremental catalogue results (IDs, not books);
- never yields its generation to the next HOT deadline;
- never holds a global scan lock.

UNIVERSE no longer needs executable books, paper decisions, or auto-capture.

The **price engine** owns full ACTIVE-catalogue economics coverage. HOT is a
priority tier inside that engine.

This is architect-supported on PR #339 **conditional on this coverage
guarantee**. It is not yet the product contract. See §11 for the required
tenet update and Phase 2 gate.

---

## 7. Proposed durable approved-market catalogue

### 7.1 Role

The catalogue is the **durable scheduler source of truth**. It joins:

- canonical fixture identity,
- Approved Match Register canonical key + structural parameters,
- exact Matchbook IDs / outcome mapping,
- exact Kalshi IDs / constituent contract IDs,
- register version, lifecycle, content version, provenance timestamps,
- invalidation state/reason,
- optional foreign key to a Kalshi fee snapshot (not the fee policy itself).

It is **not** a second matcher. `registered_canonical_key` remains the only
runtime equivalence function. Admission (`PAPER_ASSUMED_EQUIVALENT` vs
`APPROVED_EQUIVALENT`), `settlement_assumption`, and `live_execution_eligible`
are **derived at use time** from the current register + `SPORTS_HEDGE_MODE=paper`.
Do not persist those policy fields on the catalogue row; they can drift.

It is **not** the UNIVERSE checkpoint. Checkpoint stays compact resume
telemetry (#334). Putting exact IDs or books into
`universe_generation_checkpoint` would reopen the fat-payload failure.

It is **not** a durable work-queue. Price-engine items are derived in process
memory from ACTIVE rows.

### 7.2 Record shape (Phase 2)

Logical row (names indicative; implementation may split fixture vs market tables):

```text
approved_market_catalogue_v1
  catalogue_row_id                  # stable primary key
  catalogue_schema_version          # integer, start 1
  register_version                  # "v1" from approved_register.REGISTER_VERSION
  register_canonical_key            # MATCH_RESULT_FT | BTTS_FT | TOTAL_GOALS_FT:{line} | FTTS_FT

  canonical_event_id
  competition                       # TARGET_COMPETITIONS key
  home_canonical
  away_canonical
  kickoff_utc

  matchbook_event_id
  matchbook_market_id
  matchbook_runner_ids              # ordered, register outcome space
  kalshi_event_ticker
  kalshi_market_tickers / constituent_contract_ids
  kalshi_series_ticker              # provenance + fee-snapshot join key; never admission

  family / period / line            # structural parameters required by the register gate
  required_outcomes                 # e.g. HOME,DRAW,AWAY — identity, not policy

  kalshi_fee_snapshot_id            # nullable FK; unknown/null ⇒ not paper-eligible

  row_state                         # ACTIVE | NOT_LISTED | TERMINAL | DISAPPEARED | INVALIDATED
  invalidation_reason               # honest, optional
  first_catalogued_at
  last_confirmed_at
  last_seen_generation_id
  content_version                   # bump when native IDs change
```

Do **not** store: `paper_admission`, `settlement_assumption`,
`live_execution_eligible`, quotes, or solver output.

Invariants:

1. Unique on `(canonical_event_id, register_canonical_key)` while `ACTIVE`.
2. Native IDs are identity. Quotes never live on this row.
3. `TOTAL_GOALS_FT:2.5` and `TOTAL_GOALS_FT:3.5` are different rows.
4. Invalidation is honest: terminal provider status, missing market on a
   subsequent UNIVERSE confirm, or structural mismatch. Do not delete history;
   append state change (Tenet 08/11).
5. Restart reconstructs the price-engine queue from ACTIVE rows **without**
   waiting for a full UNIVERSE re-list, then lazily confirms.
6. Unregistered families never get a row.

### 7.3 Confirmation / invalidation

UNIVERSE, per fixture, on a bounded catalogue pass:

```text
if no ACTIVE row → list four-family markets → register → insert ACTIVE
                   + capture/refresh Kalshi fee snapshot
if ACTIVE row → optional cheap confirm (same IDs still listed)
if listed IDs changed → bump content_version; price engine must not use old IDs
if market disappeared / event terminal → DISAPPEARED or TERMINAL;
                   derived items drop
```

The price engine never invents IDs. If get_market / order_book says gone:
`hot_revalidation_needed` and a catalogue confirm is requested. It does not
list the whole event as a shortcut.

---

## 8. Price-engine work: process-memory, derived from the catalogue

### 8.1 No durable work-item state machine

Do **not** persist `hot_work_item_v1` with QUEUED / IN_FLIGHT / RETRY_WAIT as
authoritative scheduler state. That would be a second checkpoint.

Default:

- durable catalogue = source of truth;
- in-process derived items for ACTIVE rows;
- in-flight leases and `(2, 5, 10)`s backoff live in process memory;
- after restart, rebuild the working set from ACTIVE rows (backoff resets);
- append-only observability may record item attempts **behind** the path.

Ephemeral in-memory view (not a table):

```text
derived_price_item
  catalogue_row_id + content_version
  canonical_event_id + register_canonical_key
  exact Matchbook / Kalshi IDs (copied from the row at claim time)
  priority                    # HOT | BACKGROUND  (from classify_scan_lane
                              # + latest decision promotion, not from SQLite)
  in_flight / next_retry_at   # process memory only
  last_error_stage / detail   # process memory; history sink may copy
```

Conceptually one item remains Issue #337’s tuple:

```text
fixture + canonical market key
+ Matchbook exact IDs
+ Kalshi exact IDs
```

A 26-fixture HOT-priority roster with two families each is 52 HOT-tier items.
The remaining ACTIVE universe (e.g. T-6d rows) is BACKGROUND-tier items on the
same engine, not “not priced.”

### 8.2 Priority tiers

| Tier | Membership | Cadence (indicative; not a timeout raise) | Provider priority |
| --- | --- | --- | --- |
| HOT | `classify_scan_lane` HOT (in-play, ≤60m pre-kickoff, bounded post-kickoff unknown) **plus** opportunity-promoted rows (Issue #200) | existing ~30s HOT cadence / 25s envelope as a **scheduling slice**, not a roster leftover budget | existing HOT `ProviderPriority` |
| BACKGROUND / CATALOGUE | every other ACTIVE four-family row | slower bounded cadence (starting point: reuse `paper_live_refresh_universe_interval_seconds` 180s as the *pricing* interval for this tier — exact setting is a Phase 3 choice). Must not starve HOT. Must not starve UNIVERSE catalogue work (anti-starve grants stay). | below HOT; still isolated per item |

Promotion: a BACKGROUND item that publishes a positive / near / qualifying
decision becomes HOT-priority **immediately**. Demotion follows existing
Issue #200 rules when merged current-state ceases to qualify and no lifecycle
HOT reason remains.

Operator UI may still say Fast scan / HOT for the frequent tier. It must also
show BACKGROUND evaluated / remaining so T-6d coverage is honest.

### 8.3 Item lifecycle (in memory)

```text
derived from ACTIVE row
  → wait until due for its tier and next_retry_at (if any)
  → fetch constituents independently through the provider gate
  → item is evaluated only when the complete required outcome set has
    fresh executable books
  → if a constituent times out/unavailable: fail this item, RETRY_WAIT in memory
  → if books complete and fees/FX known: scan_pair
  → publish decision immediately
  → if paper-eligible and autofill ON: persist_triggered_chain immediately
  → evaluated means priced, not “is an arb”
```

Unevaluated must not be the default outcome of a sibling’s timeout.

### 8.4 Multi-contract atomicity and provider slots

A canonical item can require several Kalshi contracts (GAME H/D/A, FTTS
HOME/AWAY/NO_GOAL, TOTAL OVER/UNDER).

Rules:

1. The item is **evaluated** only when every required outcome has a fresh
   executable book. Partial books are not a paper decision.
2. Each constituent `order_book` / Matchbook `get_market` uses the shared
   provider gate **independently** (one slot per in-flight HTTP).
3. Do **not** reserve or hold a set of slots (e.g. 3 Kalshi + 1 Matchbook)
   while waiting for the other venue. That is a cross-provider deadlock /
   slot hostage.
4. Fetch order: take a slot, make one call, release/hold-until-task for that
   call only, then take the next. Assembly happens after returns.
5. One missing or timed-out constituent fails **that item** truthfully
   (`order_book_timeout after 8s` on the source_id). Sibling items continue.
6. If all 4 Kalshi slots are genuinely occupied by slow calls, further items
   wait with `provider_capacity_saturated` / `deferred` (Tenet 19 local
   wait ≠ venue outage). Do **not** mislabel the remaining queue as
   `scan_budget_exhausted`.
7. HOT-tier items keep existing request priority vs UNIVERSE; BACKGROUND
   items must not starve UNIVERSE catalogue `list_markets` (anti-starve
   grants still apply at the provider gate).

### 8.5 Retry / backoff ownership

| Failure | Owner | Action |
| --- | --- | --- |
| `get_market_timeout after 8s` / `order_book_timeout after 8s` | that derived item | in-memory `RETRY_WAIT` using `(2, 5, 10)`s; do not leftover the roster |
| HTTP still running after wait (lease hold) | that call’s lease | one slot occupied; **other items use remaining slots** |
| All 4 Kalshi slots busy | provider gate | `provider_capacity_saturated` / `deferred` |
| Rate limited / cooldown | provider gate | defer item; not venue outage |
| Structural mismatch / identity changed | catalogue | invalidate or bump `content_version`; drop derived item |
| Missing/partial Kalshi fee metadata | economics snapshot | item not paper-eligible; fail closed; do not invent 0% |
| Stale quotes | freshness gate | fail closed for capture; retry next cadence |
| UNIVERSE `list_markets_timeout` | that fixture’s catalogue pass | retry catalogue unit; do not mark already-ACTIVE rows exhausted |
| Hung UNIVERSE chunk | #330 watchdog | chunk ends; epoch quarantines; generation resumes; **price engine keeps running** |
| Process restart | — | rebuild from ACTIVE rows; item backoff resets (safe; see §0.5) |

Do not convert transients to `FINAL_FAILED` solely because
`paper_universe_work_max_attempts` is reached.

### 8.6 Concurrency (explicit non-goals)

Keep current caps unless a later phase **proves** provider/runtime safety:

- Matchbook 4, Kalshi 4;
- HOT-tier priority with starvation grants = 8 versus UNIVERSE;
- provider timeout 8s, discovery 15s;
- HOT-tier scheduling slice may keep the 25s envelope as a *yield*, not as
  leftover-as-exhausted for unstarted items.

The coverage win comes from **isolation + BACKGROUND coverage**, not from more
simultaneous HTTP.

Unstarted items remain due/queued (honest `not_started_this_cadence`), not
`scan_budget_exhausted`.

### 8.7 What “one slow call” looks like after Phase 3

```text
Price engine due
  HOT tier: 26 fixtures × 2 families = 52 items
  BACKGROUND tier: remaining ACTIVE four-family rows (including T-6d)
Kalshi slots = 4
Item 7 (HOT) order_book times out at 8s
  → item 7 in-memory RETRY_WAIT now+2s
  → HOT items 8–52 still eligible
  → BACKGROUND items still eligible when HOT slice yields
  → operator: evaluated K / (HOT+BACKGROUND due), 1 retry_wait
     not 0/26 leftover
A T-6d BACKGROUND item that was mid-fetch is not cancelled by item 7.
```

---

## 9. Kalshi economics metadata (required in the target, not quotes)

Owner-live also exposed `missing costs` / `unknown_required_venue_cost:kalshi`.
Matchbook commission is registry-backed (`paper_account_fees` SQLite). Kalshi
economics come from official event/series metadata via
`fees/kalshi.py` `resolve_kalshi_fee_metadata`:

- complete event override (`fee_type_override` + `fee_multiplier_override`)
  replaces series `fee_type` / `fee_multiplier`;
- otherwise series values;
- **partial** override combinations fail closed and never silently fall back;
- flat / unknown `fee_type` fail closed;
- missing metadata is not zero.

If Phase 2 stops repeating Get Series / Get Market for settlement re-proof,
the price engine still needs those fee fields. Put them in a **separate
compact snapshot table**, referenced by the catalogue. Do not bloat the
catalogue row with quotes or a second policy decision.

### 9.1 Snapshot shape

```text
kalshi_fee_snapshot_v1
  snapshot_id
  series_ticker                     # join key
  event_ticker                      # nullable; event override lives here
  market_ticker                     # nullable if series/event is sufficient
  fee_type                          # quadratic | quadratic_with_maker_fees | null
  fee_multiplier
  fee_type_override                 # raw, for audit
  fee_multiplier_override
  series_fee_type / series_fee_multiplier
  fee_provenance                    # series | event_override
  fee_resolution_status             # known | unknown | partial_event_fee_override | unsupported
  fee_resolution_error              # e.g. partial_event_fee_override
  captured_at
  confirmed_at
  source                            # nested list_events | get_series | event payload
```

### 9.2 Capture, use, invalidation

- UNIVERSE captures/refreshes the snapshot when it lists Kalshi series/events
  for catalogue work (those payloads already carry fee fields). That is
  **catalogue-time**, not a HOT Get Series loop.
- Catalogue ACTIVE rows store `kalshi_fee_snapshot_id` when resolution_status
  is `known`. Null or non-known ⇒ paper-ineligible (fail closed).
- Price engine loads the snapshot by id / series+event key. It does not Get
  Series merely to re-prove settlement. It does not treat a stale quote cache
  as a fee snapshot.
- Invalidation: UNIVERSE confirm sees different type/multiplier/override → new
  snapshot id; old row superseded; items using the old id must not stay
  paper-eligible.
- Refresh cadence: confirm with catalogue confirmation; do not poll Get Series
  on the HOT 30s path.
- Unknown/partial/unsupported stays fail-closed. Never invent `fee_multiplier=0`.

FX remains the existing FX service snapshot. Matchbook remains the account-fee
registry. Both are fail-closed if missing.

---

## 10. Economics and paper decision at item completion (Phase 4 — do not implement now)

When **one** price-engine item has a complete fresh outcome set + known economics:

1. calculate net edge;
2. publish decision immediately into current-state / watchlist projection;
3. if eligible and AUTO PAPER CAPTURE ON, call `persist_triggered_chain` immediately;
4. persist durable OPEN or explicit rejection;
5. if BACKGROUND and qualifying/positive-near, promote to HOT priority immediately.

Do **not** wait for the rest of the HOT slice, BACKGROUND slice, or UNIVERSE
chunk.

### 10.1 Coordination with merged Issue #336 (mandatory)

#336 / PR #338 merged to `owner-live` as `4060ef151f3c7cb22806c8e83ee78abfb5714df4`.

Contract now in production:

- `paper_assumed_equivalent` is a non-blocking scan/audit label, not watchlist leftover `REJECTED`;
- capture either OPENs or records durable `PAPER_FILL_REJECTED` with the exact reason;
- process-cached `paper_autofill_enabled` refreshes from `get_settings()`;
- freshness / treasury / complete-opening / idempotency stay fail-closed.

Phase 4 **must**:

- reuse `persist_triggered_chain`;
- persist OPEN **or** `PAPER_FILL_REJECTED` (do not invent a third rejection type;
  `PaperChainStep.PAPER_ENTRY_REJECTED` remains an enum alias, not a parallel path);
- **not** reimplement watchlist leftover classification.

---

## 11. Formal Tenet 19 contract update (Phase 2 precondition)

This RFC is **not** itself a tenet change. An architect comment is not a
permanent product contract.

**Gate:** no Phase 2 runtime code until
`docs/core-tenets/19_CONCURRENT_HOT_AND_UNIVERSE_SCANNING.md` (and the
one-line summary in `docs/CORE_TENETS.md` item 19) is updated to the effect
below, either as a stacked first commit of Phase 2 or a preceding docs PR.

### 11.1 Proposed replacement of the architectural contract sentence

Current (Tenet 19 intro):

```text
UNIVERSE discovers. HOT watches. Execution decides.
```

Proposed:

```text
UNIVERSE catalogues. The price engine prices every ACTIVE catalogue row.
HOT is a priority tier inside that engine. Execution decides.
```

### 11.2 Proposed non-negotiable bullets to add (or replace §6–§7 evaluation language)

```text
- UNIVERSE owns durable catalogue completeness for the approved Matchbook↔Kalshi
  families (fixture identity, register key, exact native IDs, invalidation).
  UNIVERSE does not need executable books to complete a fixture’s catalogue job.
- The price engine owns full-universe economics coverage: every ACTIVE supported
  catalogue row is eventually refreshed and evaluated, including rows that have
  never shown an edge and rows outside the HOT lifecycle horizon.
- HOT is a priority tier (in-play / near-kickoff / already-interesting /
  opportunity-promoted) with frequent cadence. BACKGROUND/CATALOGUE priority
  covers the remaining ACTIVE rows at a slower bounded cadence.
- A positive or qualifying BACKGROUND decision promotes that row to HOT
  priority immediately. Promotion does not stop UNIVERSE.
- HOT and UNIVERSE remain independently scheduled concurrent workers.
  No leftover-until-HOT. No global scan lock.
- Provider timeout isolation applies to both price-engine tiers. One slow
  call fails one item. Shared collector remaining_soft must not leftover-mark
  unrelated work as scan_budget_exhausted.
- Provider slot saturation is reported as deferred/capacity, not as venue
  outage or scan-budget failure.
```

Keep unchanged: paper-only, register authority, chunk/epoch #330, compact
checkpoint #334, anti-starve grants, truthful operation-specific health.

Existing Tenet 19 tests that HOT can overlap UNIVERSE and that UNIVERSE
progress is durable remain required. Add the coverage tests in §13.2.

---

## 12. Observability behind the critical path (Phase 5 — do not implement now)

Move or keep as asynchronous consumers:

- append-only paper-scan audit and item-attempt history (not scheduler authority);
- UI aggregation (`FixtureCurrentStateStore` becomes a projection of item
  events + catalogue state);
- UNIVERSE compact checkpointing (already off-loop);
- health/read models;
- provider diagnostics.

Rules:

- No status endpoint rebuilds expensive runtime state per poll.
- `/health` stays independent of scanner load (#334).
- Counters: HOT-tier vs BACKGROUND-tier queued / in-flight / evaluated /
  retry_wait / not_started_this_cadence, plus operation-specific health.
- `scan_budget_exhausted` disappears from the price-engine path.
- Capacity waits say `provider_capacity_saturated` / `deferred`.

---

## 13. Exact tests required for later phases

Clock-injected; no live HTTP in CI. Owner-live soak is Phase 6, not CI.

### 13.1 Phase 2 — durable catalogue + fee snapshot

1. UNIVERSE persist of all four keys for a synthetic MB↔K fixture with exact native IDs.
2. `TOTAL_GOALS_FT:2.5` and `TOTAL_GOALS_FT:3.5` are distinct rows; 2.5↔3.5 never ACTIVE as one key.
3. Integer/quarter totals never catalogued as `TOTAL_GOALS_FT`.
4. Extra-time / to-qualify / incomplete HDA / missing FTTS `NO_GOAL` never ACTIVE.
5. DNB/handicap/DC/team-total/player props produce no catalogue row and no Kalshi depth.
6. Register version is stored; scanner still calls `registered_canonical_key`, not confidence.
7. Catalogue row has **no** `paper_admission` / `settlement_assumption` /
   `live_execution_eligible` columns; admission is derived from the live register + PAPER mode.
8. Disappeared Kalshi BTTS invalidates that row honestly; other families on the fixture stay ACTIVE.
9. Terminal Matchbook event marks rows TERMINAL; derived items drop.
10. Process restart loads ACTIVE IDs without `list_markets` and reconstructs the price-engine working set.
11. Compact checkpoint remains ≤256 KiB and still rejects `report` / `discovery_snapshot` keys.
12. Kalshi fee snapshot stores `fee_type` / `fee_multiplier` / provenance / resolution status;
    partial override ⇒ not `known`; price engine fail-closes (no zero fee).
13. Fee snapshot contains no quotes. Catalogue FK to an unknown snapshot ⇒ not paper-eligible.
14. Existing #331 / #326 / #324 four-family tests stay PASS.
15. `execution_enabled=false`; venue modules still have no place/cancel/sign.
16. Tenet 19 contract file updated per §11 **before or with** this phase.

### 13.2 Phase 3 — price engine (HOT + BACKGROUND) + isolation

1. One `order_book_timeout after 8s` fails only that work item; sibling items still evaluate.
2. Shared collector `remaining_soft` cannot leftover-mark unstarted items as `scan_budget_exhausted`.
3. Unstarted items remain `not_started_this_cadence`.
4. Four busy Kalshi slots surface `provider_capacity_saturated` / `deferred`, not scan-budget failure.
5. Provider slot caps still 4/4; a held lease blocks one slot, not the queue.
6. No cross-venue slot hostage: an item waiting on Kalshi does not hold a Matchbook slot.
7. An item requiring H/D/A is not marked evaluated until all three constituent books are fresh; one timed-out constituent fails that item only.
8. Price engine skips `list_events` / `list_markets` when catalogue IDs exist.
9. Missing catalogue row → no silent full-event listing; confirm request.
10. Retry backoff (2, 5, 10) is per in-memory item; no durable queue table; restart resets backoff and still prices ACTIVE rows.
11. HOT-tier work can run while UNIVERSE is cataloguing (#330 overlap tests stay PASS).
12. Stale UNIVERSE chunk callbacks still cannot mutate checkpoint/catalogue (#330 epoch).
13. **Coverage (architect-required):**
    1. an outside-HOT-horizon ACTIVE row is still BACKGROUND-priced;
    2. it develops a qualifying edge;
    3. the decision emits immediately (does not wait for the rest of the roster or for kickoff);
    4. it is promoted to HOT priority without waiting for kickoff/lifecycle;
    5. an unrelated item timeout cannot stop that BACKGROUND evaluation.
14. HOT-tier priority still yields to UNIVERSE after 8 consecutive grants; BACKGROUND must not starve catalogue `list_markets`.
15. Existing dual-cadence / HOT targeted-refresh tests updated, not deleted.

### 13.3 Phase 4 — economics + capture at item completion

1. Decision event emitted when the item finishes, before other items in the cadence complete.
2. Autofill ON + eligible → OPEN through `persist_triggered_chain`; repeated observations idempotent.
3. Autofill ON + not eligible → durable `PAPER_FILL_REJECTED` with exact reason; no silent skip (merged #336).
4. Autofill OFF → no OPEN; decision still published.
5. Unknown/partial Kalshi fees or missing Matchbook commission → not eligible, fail closed, no invented 0%.
6. Quote age ≥ `paper_entry_max_quote_age_ms` → no OPEN.
7. Merged #336 watchlist leftover / `paper_assumed_equivalent` tests stay PASS.
8. Demo/fixture replay still does not inherit live autofill.
9. Persist/auto-capture failure does not count as `scan_cycle_timeout`.
10. BACKGROUND qualifying capture still idempotent and still promotes HOT priority.

### 13.4 Phase 5 — observability

1. `/health` and `/build-info` remain dispatchable during HOT + BACKGROUND + UNIVERSE load.
2. `/paper/live-refresh` does not call `collect_and_scan` or open a fat checkpoint.
3. Status shows HOT-tier vs BACKGROUND-tier evaluated/retry counts independently of leftover-assembly.
4. Lane + operation health: HOT Kalshi `order_book` timeout does not display as MATCHBOOK FAILED.
5. Audit/history consumers can lag without blocking the next item.

### 13.5 Phase 6 — rollout

1. Synthetic old vs new: identical register keys and native IDs for locked fixtures.
2. Owner-live PAPER soak: high % of **ACTIVE** rows evaluated (not merely HOT-membership rows); timeout isolation; HOT cadence during UNIVERSE; BACKGROUND→HOT promotion; capture determinism; no execution.
3. Fallback: empty catalogue → UNIVERSE insert path → derived items, no dual matcher.

---

## 14. Migration / compatibility

Preserve existing owner-live SQLite where safe:

| Store | Migration |
| --- | --- |
| `universe_generation_checkpoint` v2 | **No payload change.** Do not add books, catalogue rows, or work-queue state. |
| Paper ledger / trades / journal | Unchanged schema for Phase 2–3. Phase 4 still writes through merged #336 persist. |
| Watchlist | Additive fields only if needed. #336 already merged. |
| Mapping rules / review DB | Untouched. Not a runtime admission gate. |
| FX / Matchbook account fees | Untouched. |
| New `approved_market_catalogue` | Additive. |
| New `kalshi_fee_snapshot` | Additive, compact, no quotes. |
| Price-engine queue | **Not persisted.** |
| `FixtureCurrentStateStore` | Process-memory projection. Restart rebuilds items from ACTIVE catalogue. |

Fallback while catalogue is empty (first boot / explicit reset):

```text
UNIVERSE behaves as today for discovery + register lookup
  → writes catalogue rows + fee snapshots as soon as IDs/metadata are known
  → price engine derives HOT + BACKGROUND items
  → no second matcher
```

Do not dual-run two equivalence authorities. Do not persist a second admission
policy. Do not keep writing `min_mapping_confidence` into runtime admission.

---

## 15. Code paths that must **not** change in Phase 1

Phase 1 is this RFC. **No production runtime, schema, matcher, collector,
coordinator, venue, paper, frontend, or tenet-file change in this PR.**

The Tenet 19 file update is a **later** gated docs change (§11), not this
revision.

In particular, do not touch in this PR:

- `backend/src/sports_hedge/**` (all production modules)
- `frontend/**`
- `docs/core-tenets/**` (until the gated §11 PR)
- tests

The only deliverable is this document.

### 15.1 Code paths later phases must treat as frozen contracts

| Path | Why frozen |
| --- | --- |
| `matching/approved_register.py` semantics | #331 sole runtime authority |
| `catalogue/registry.py` four-family paper flags | #326 product matrix |
| `scan_lanes.universe_chunk_wall_seconds` ignoring `next_hot_due` | Tenet 19 / #330 |
| UNIVERSE chunk epoch quarantine in `live_refresh.py` | #330 |
| Compact checkpoint v2 + 256 KiB + off-loop persist | #334 |
| Cheap `/health` / `/build-info` | #334 |
| `ReadOnlyVenue` (no place/cancel) | Tenet 02 |
| `persist_triggered_chain` + merged #336 leftover/rejection behaviour | compose, don’t fork |
| `fees/kalshi.py` fail-closed resolution (partial override, no zero) | Tenet 15 |
| `paper_entry_max_quote_age_ms` fail-closed | Tenet 18 |
| Provider slot caps / 8s provider timeout **as the first lever** | Issue #337 non-negotiable |
| Runtime `min_mapping_confidence` / learned rules / mapping-review as admission | Tenet 20 |
| Dislocation burst scheduler import into HOT | Dual-cadence v1 non-goal |

---

## 16. Phased program (implementation forbidden in this PR)

| Phase | Goal | Runtime change? |
| --- | --- | --- |
| **1** | This RFC | **No** |
| 1b | Tenet 19 contract update (§11) | Docs only; **required before Phase 2 code** |
| 2 | Durable catalogue + Kalshi fee snapshots | Yes, additive persistence + UNIVERSE writer |
| 3 | Price engine: HOT + BACKGROUND derived queue, isolation, multi-contract rules | Yes |
| 4 | Economics + capture at item completion | Yes; uses merged #336 |
| 5 | Observability as consumers | Yes |
| 6 | Rollout / soak / fallback | After 2–5 |

Each later phase is its own draft PR against then-current `owner-live`, with
Tenet 12 review, and must list whether data shown is live, historical, modelled,
or fixture/demo.

---

## 17. Explicit non-goals

- Raising `paper_scan_provider_timeout_seconds` or HTTP read timeouts as the fix.
- Broadly increasing Matchbook/Kalshi concurrency.
- Reintroducing runtime market-equivalence confidence or REVIEW_REQUIRED admission for registered rows.
- Putting exact books, discovery snapshots, or `CollectionReport` back into the UNIVERSE checkpoint.
- A durable price-engine work-queue / `next_retry_at` checkpoint.
- Persisting `paper_admission` / `settlement_assumption` / `live_execution_eligible` on catalogue rows.
- Putting quotes in the catalogue or fee snapshot, or inventing zero Kalshi fees.
- A second canonical identity system or a HOT-only matcher.
- Exclusive HOT-only pricing that leaves T-6d ACTIVE rows unpriced.
- Live execution, Smarkets, or expanding the paper register beyond the four families.
- Replacing merged #336 persist with a new capture service.
- Yielding UNIVERSE to HOT (pre-Tenet-19 leftover-until-HOT stays superseded).
- Using Research probabilities as arb admission.
- Treating this RFC’s architect-review comment as the Tenet 19 file.

---

## 18. Tenet 12 review (this RFC revision)

**Applicable tenets:** 02, 03, 04, 09, 10, 11, 12, 14, 15, 18, 19, 20.

**Satisfied by this design (not yet by code):**

- 02 — paper/read-only; no venue write in any proposed phase.
- 03 / 20 — register remains sole registered-row authority; catalogue stores IDs; policy is derived, not copied.
- 04 / 11 — leftovers and timeouts become item-truthful; radar ≠ executable; BACKGROUND coverage is visible.
- 09 / 10 / 15 / 18 — capture still fail-closed on fees/FX/freshness/allocator/idempotency; Kalshi fee snapshots are first-class.
- 14 — HOT cadence plus BACKGROUND coverage so dislocations and pre-horizon edges can surface.
- 19 — workers stay concurrent; coverage guarantee preserved via the price engine; formal file update gated in §11.
- 12 — this review.

**Partial / deferred:**

- Implementation Phases 2–6.
- Tenet 19 markdown update (§11) — required before Phase 2 runtime, not done in this PR.
- Polymarket remaining on default lane venue lists until Phase 3 scheduling scope.

**Potential conflicts:**

- None accepted against paper-only, #330, #331, #334, or merged #336.
- Tenet 19 §6–§7 evaluation language: **explicit gated update**, not a silent weaken.
- Exclusive HOT-only pricing (previous draft) is withdrawn; it conflicted with Tenet 19 coverage.

**Data honesty:**

- This document is design-only.
- Timeout/budget figures are **code defaults** on `4060ef151f3c7cb22806c8e83ee78abfb5714df4` (unchanged from `45d68d2` for those knobs).
- Owner-live 68/9/56 and HOT 0/26 figures are **operator-cited symptoms** from Issue #337, not reproduced by this RFC.
- `missing costs` is cited as owner-live debugging context from the architect review, not as a new measurement in this PR.
- No live books, historical odds, modelled probabilities, or demo fixtures are shown as current.

**Safety:**

- Phase 1 paper-only / read-only venue boundary preserved.
- No production code changes in this PR.

---

## 19. Recommended implementation order after architect acceptance of this revision

1. Accept this RFC, including full-universe price-engine coverage and the catalogue-derived (non-durable) queue.
2. Land the Tenet 19 contract update (§11) — docs-only, required gate.
3. Phase 2 catalogue + Kalshi fee snapshots.
4. Phase 3 price engine (HOT + BACKGROUND isolation, multi-contract slot rules).
5. Phase 4 item-completion economics + merged #336 `persist_triggered_chain`.
6. Phase 5 item-level status; delete leftover-as-exhausted from the price path.
7. Phase 6 synthetic compare + owner-live PAPER soak.

Stop for architect review at each phase. Do not merge this RFC as if it were runtime.
