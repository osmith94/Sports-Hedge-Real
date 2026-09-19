# Scanner simplification RFC — durable catalogue, independent HOT pricing, background controls

**Issue:** #337
**Phase:** 1 — architecture / RFC only
**Status:** Draft for architect review. Do not implement Phases 2–6 in this PR.
**Date:** 19 September 2026
**Base:** current `owner-live` `45d68d270246ec6a0b351f78562fae4244bf4d2f`  
  (`Owner-live: compact off-loop UNIVERSE checkpoints`)
**Mode:** PAPER MODE · EXECUTION DISABLED
**Data class:** architecture/design from current code, existing tenets, and cited owner-live operator symptoms. Not live quotes. Not modelled probabilities. Not fixture/demo UI data.

This document does **not** change production runtime behavior.

> **Product rule this program exists to satisfy:**
>
> UNIVERSE maintains a durable approved-market catalogue.
> HOT is an exact-ID price engine over independent work items.
> Audit, history, UI aggregation, checkpointing and diagnostics consume
> events **behind** that path. Fail-closed safety is not optional.

Related live contracts this RFC must preserve, not reopen:

| Contract | Issue / PR | Authority |
| --- | --- | --- |
| Paper / read-only venue boundary | Tenet 02 | `SPORTS_HEDGE_MODE=paper`, `SPORTS_HEDGE_EXECUTION_ENABLED=false` |
| Concurrent HOT + UNIVERSE workers | Tenet 19 | HOT must never cancel/reset UNIVERSE; no leftover-until-HOT time-slicing |
| Approved Match Register | #331 / Tenet 20 | Sole runtime market-equivalence authority for registered rows |
| UNIVERSE chunk watchdog + epoch quarantine | #330 | Bounded chunk, unbounded generation, stale callbacks quarantined |
| Compact off-loop UNIVERSE checkpoint | #334 / PR #335 | Resume telemetry only; ≤256 KiB; not on the event loop |
| Paper-eligible auto-capture outcome | #336 / open PR #338 | Durable OPEN **or** explicit rejection; do not duplicate or contradict |

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
4. Enqueue one HOT work item per (fixture, canonical market key, exact IDs).
5. Refresh those exact books.
6. Apply already-known fees/FX (fail closed if unknown).
7. Solve and publish the paper decision immediately.
8. If AUTO PAPER CAPTURE is ON and the item is eligible: attempt capture immediately,
   persisting OPEN or an explicit rejection. Never silently vanish.
```

Everything else — full-event market census, unsupported-family depth, cycle-end audit
fan-out, UI aggregation, compact UNIVERSE resume checkpoints, mapping-review stores —
is either onboarding/diagnostics or an asynchronous consumer.

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
refresh, known fees/FX, freshness, allocator/treasury gates, paper-entry idempotency.

### 0.3 Which current operations can HOT stop doing once exact IDs are catalogued?

HOT already skips `list_events` and `list_markets` / `get_series` rediscovery when
`hot_market_relationships` are present (`collector.py` HOT path; Issue #320/#318).
After a durable catalogue exists, HOT can also stop:

- depending on process-memory `FixtureCurrentStateStore` as the only ID source;
- re-running greedy market pairing / `MarketMatcher` as a discovery step;
- treating a missing relationship as a reason to list the whole event;
- sharing one cycle `remaining_soft` across unrelated fixtures so one
  `order_book_timeout after 8s` leftover-marks the rest of the roster;
- waiting for a whole HOT batch to finish before publishing a decision.

HOT must still re-validate that persisted IDs still resolve (gone / identity-changed
→ `hot_revalidation_needed`) and must still fail closed on stale books.

### 0.4 How should one slow Kalshi/Matchbook call affect only one work item?

Give each HOT item its **own** provider timeout and retry state.

A Kalshi `order_book` that hits `paper_scan_provider_timeout_seconds` (default **8s**)
must:

- fail that work item (`order_book_timeout after 8s`, truthful, retryable);
- release that item’s claim on a Kalshi slot when the lease ends;
- leave every other queued item eligible to run within the same HOT cadence;
- **not** drive collector `remaining_soft` to 0 for the rest of the roster;
- **not** leftover-mark unrelated fixtures as `scan_budget_exhausted`.

Do **not** raise the 8s provider timeout or the 15s discovery timeout as the
primary fix. Do **not** broadly raise Matchbook/Kalshi concurrency (defaults 4/4)
without provider-safety evidence.

### 0.5 Which state must be durable vs process-memory/cache?

| State | Today | Target |
| --- | --- | --- |
| Approved Match Register (archetype → canonical key) | code constant `REGISTER_VERSION=v1` | unchanged; still code |
| Exact native market/contract IDs | process-memory `HotMarketRelationship` | **durable catalogue** (new) |
| UNIVERSE generation cursor / evaluated IDs / work units | compact SQLite checkpoint v2 | unchanged resume telemetry |
| Raw `discovery_snapshot` / full `CollectionReport` | in-memory only (#334) | stay in-memory |
| Fee snapshots / FX snapshots | SQLite fee store + FX DB; Kalshi fee meta may cache on HOT leg | prepared/cacheable inputs; still fail closed if unknown |
| Paper OPEN / rejection | paper ledger + watchlist | durable at item completion (#336 contract) |
| Radar current-state / TTL merge | process-memory `FixtureCurrentStateStore` | process-memory OK; catalogue rebuilds identity after restart |
| Provider backoff / in-flight leases | process-memory | process-memory + durable item `next_retry_at` |

### 0.6 How do we migrate without breaking #330 / #331 / #334 / #336?

Additive catalogue + work-queue tables. No rewrite of the register. No fattening
of the UNIVERSE checkpoint. No second matcher. No change to paper-only venue
contracts. Auto-capture continues to call `persist_triggered_chain`; Phase 4 only
changes **when** that call runs (item completion vs cycle-end batch) and must
land on top of #336’s “OPEN or explicit rejection” contract, not beside it.

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
| 19 Concurrent workers | HOT and UNIVERSE overlap. No leftover-until-HOT. This RFC **refines** UNIVERSE “evaluate” to catalogue evaluation; solver evaluation becomes a HOT work-item. Architect must accept that refinement explicitly. |
| 20 Approved catalogue | Four locked families only on the MB↔K paper path. No runtime confidence/review admission. |
| `docs/DUAL_CADENCE_SCANNER.md` | Historical lane/TTL/Tracked contract. Tenet 19 overrides leftover-until-HOT chunking. |
| `docs/PHASE1_COMMON_MARKET_CATALOGUE_CENSUS_V3.md` | Four-family paper target. |
| `matching/approved_register.py` | Runtime register. |

Non-applicable for this program’s critical path: Research (05–08, 13, 17),
external/manual Polymarket legs as a reason to keep Polymarket on the MB↔K
HOT queue (16 still applies if a future item has an external leg).

**Known architect decision required (not a silent conflict):**
Tenet 19 §6–§7 currently describe UNIVERSE as also comparing markets and
streaming near/positive/qualifying opportunities. This RFC keeps incremental
streaming and immediate HOT promotion, but moves **economics/solver/paper
decision** off the UNIVERSE critical path and onto HOT work items. UNIVERSE
still streams catalogue rows and promotions; it does not need fresh books to
finish a fixture’s catalogue job.

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

### 3.2 Timeout / budget ownership (measured from this head)

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

Open #336 (PR #338, based on this same `owner-live` head) diagnoses a different
bug on the same handoff: paper-assumed eligible rows can vanish between scan
audit and persistence (watchlist leftover classification, swallowed
`begin_paper_fill_attempt` `ValueError`). This RFC must not fork a third capture
path. See §11.

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

### 4.4 UNIVERSE doing HOT’s job

`collect_and_scan` is still a full paper scan for both lanes: books, fees, solver,
decisions. UNIVERSE therefore spends Kalshi order-book slots and soft budget on
economics during catalogue maintenance. Under Tenet 19 that was “evaluate while
you are there.” Under owner-live load it prevents the catalogue from being
maintained.

**Target:** UNIVERSE persists exact IDs and structural metadata. It may record
“family present / not listed / disappeared.” It must not need a fresh Kalshi
book to mark a fixture catalogued.

### 4.5 Cycle-end persistence and auto-capture

Audit + `persist_triggered_chain` already run **after** the collector envelope
(good: they must not count as `scan_cycle_timeout`). They still wait for the
**batch**. Combined with 4.1, capture never runs because the batch never
evaluates the item.

#336 is the correctness of that handoff (no silent vanish). This RFC’s Phase 4
is the **timing** of that handoff (item completion). Do not implement Phase 4
by copying PR #338.

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
diagnostics/census venue; it must not consume HOT work-item slots for the four
approved families.

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
6. Known venue fees + FX before eligibility. Unknown must not become zero.
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
8. Restart-time rebuild of radar TTL cosmetics (catalogue IDs are enough to resume HOT pricing).

---

## 6. Target conceptual model

```text
DISCOVER FIXTURES
      ↓
DETERMINISTIC FIXTURE MATCH
      ↓
APPROVED MATCH REGISTER
      ↓
STORE EXACT NATIVE MARKET/CONTRACT IDS     ← UNIVERSE (catalogue maintainer)
      ↓
HOT WORK QUEUE                             ← one item per fixture+canonical key
      ↓
REFRESH EXACT BOOKS                        ← HOT (price engine)
      ↓
CALCULATE ECONOMICS                        ← prepared fees/FX + fresh books
      ↓
PUBLISH DECISION IMMEDIATELY
      ↓
AUTO PAPER CAPTURE OR EXPLICIT REJECTION   ← #336 contract, per item

Audit / history / UI / checkpoint / diagnostics consume events behind this path.
```

### 6.1 Target-path diagram

```text
                         Sports Hedge backend (paper / read-only)
                                      │
              ┌───────────────────────┴────────────────────────┐
              │                                                │
     UNIVERSE catalogue worker                         HOT price-engine worker
     persistent generation                             independent cadence ~30s
     chunk watchdog #330 preserved                     no self-overlap envelope
              │                                                │
     list_events (MB ∥ Kalshi series)                   dequeue N work items
     canonical fixture match                           bounded concurrency
     for each new/dirty fixture:                       (unchanged slot caps)
        list_markets / nested four families                    │
        register structural key                        per item:
        upsert durable catalogue row                     MB get_market(exact)
        enqueue/refresh HOT work item                    K  order_book(exact)
        invalidate disappeared/terminal                  fees/FX snapshot
     do NOT require order books                          scan_pair
     do NOT wait for HOT                                 emit decision event
              │                                          if eligible ∧ autofill:
              │                                            persist_triggered_chain
              │                                            OPEN or explicit reject
              │                                                │
              └───────────────────────┬────────────────────────┘
                                      │
                    Shared ProviderAccessLayer
                    MB 4 / Kalshi 4, HOT priority, anti-starve
                    item timeout holds one lease, not the roster
                                      │
                    Durable approved-market catalogue (new)
                    Compact UNIVERSE checkpoint (existing, unchanged role)
                    Paper ledger / watchlist / audit (consumers)
                    FixtureCurrentStateStore (radar projection)
```

### 6.2 Minimum runtime for the four families

Per fixture, UNIVERSE attempts **only**:

| Canonical key | Matchbook native | Kalshi native | Structural gate |
| --- | --- | --- | --- |
| `MATCH_RESULT_FT` | Match Odds / Final Result | GAME | FULL_TIME + HOME/DRAW/AWAY |
| `BTTS_FT` | Both Teams To Score | BTTS | FULL_TIME + YES/NO |
| `TOTAL_GOALS_FT:{line}` | Total Goals | TOTAL | FULL_TIME + exact safe half-line + OVER/UNDER |
| `FTTS_FT` | First Team To Score | FTTS | FULL_TIME + HOME/AWAY/NO_GOAL |

If Kalshi does not list that family for the fixture: catalogue row
`NOT_LISTED` / `VENUE_UNAVAILABLE`. No depth, no solver, no HOT item.

If Matchbook lists extras (DNB, handicap, player props): discard immediately.
Do not enqueue HOT work. Do not fetch Kalshi books for them.

HOT membership still follows `classify_scan_lane` (in-play, pre-kickoff horizon,
opportunity promotion). Catalogue existence is what HOT **prices**; lane
classification is what HOT **prioritises**. A T-6d catalogued row can sit on
radar without occupying the HOT queue until promotion/lifecycle says so.

### 6.3 Tenet 19 refinement (explicit)

UNIVERSE still:

- discovers;
- matches fixtures;
- streams incremental catalogue results;
- promotes fixtures into HOT **watch** when catalogue rows exist or lifecycle says so;
- never yields its generation to the next HOT deadline;
- never holds a global scan lock.

UNIVERSE no longer needs to:

- fetch executable books for solver admission;
- emit paper decisions;
- auto-capture.

That work becomes HOT work-item completion. Architect sign-off on this
refinement is a Phase 2 entry criterion.

---

## 7. Proposed durable approved-market catalogue

### 7.1 Role

The catalogue is the **durable join** between:

- canonical fixture identity,
- Approved Match Register canonical key,
- exact Matchbook IDs,
- exact Kalshi IDs.

It is **not** a second matcher. `registered_canonical_key` remains the only
runtime equivalence function. The catalogue stores the **result** of a successful
register lookup plus native IDs.

It is **not** the UNIVERSE checkpoint. Checkpoint stays compact resume
telemetry (#334). Putting exact IDs into `universe_generation_checkpoint`
would reopen the fat-payload failure.

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

  matchbook_event_id                # native
  matchbook_market_id               # native
  matchbook_runner_ids              # ordered, register outcome space
  kalshi_event_ticker
  kalshi_market_tickers             # YES contracts / constituent_contract_ids
  kalshi_series_ticker              # provenance only; never admission

  family / period / line            # structural copy of the register gate
  required_outcomes                 # e.g. HOME,DRAW,AWAY
  paper_admission                   # PAPER_ASSUMED_EQUIVALENT | APPROVED_EQUIVALENT
  settlement_assumption             # regulation_time for paper-assumed rows
  live_execution_eligible           # always false in Phase 1

  row_state                         # ACTIVE | NOT_LISTED | TERMINAL | DISAPPEARED | INVALIDATED
  invalidation_reason               # honest, optional
  first_catalogued_at
  last_confirmed_at
  last_seen_generation_id           # UNIVERSE generation that last confirmed IDs
  content_version                   # bump when native IDs change
```

Invariants:

1. Unique on `(canonical_event_id, register_canonical_key)` while `ACTIVE`.
2. Native IDs are identity. Quotes never live on this row.
3. `TOTAL_GOALS_FT:2.5` and `TOTAL_GOALS_FT:3.5` are different rows.
4. Invalidation is honest: terminal provider status, missing market on a
   subsequent UNIVERSE confirm, or structural mismatch. Do not delete history;
   append state change (Tenet 08/11).
5. Restart must be able to enqueue HOT items from ACTIVE rows **without**
   waiting for a full UNIVERSE re-list, then lazily confirm.
6. Unregistered families never get a row.

### 7.3 Confirmation / invalidation

UNIVERSE, per fixture, on a bounded catalogue pass:

```text
if no ACTIVE row → list four-family markets → register → insert ACTIVE + enqueue HOT
if ACTIVE row → optional cheap confirm (same IDs still listed)
if listed IDs changed → bump content_version, enqueue revalidation item, do not
                        price the old IDs
if market disappeared / event terminal → row_state DISAPPEARED or TERMINAL,
                        drop or hold HOT items
```

HOT never invents IDs. If get_market / order_book says gone:
`hot_revalidation_needed` and a catalogue confirm is requested. HOT does not
list the whole event as a shortcut.

---

## 8. Independent HOT work items, retry, backoff

### 8.1 Work-item shape (Phase 3)

```text
hot_work_item_v1
  work_item_id
  canonical_event_id
  register_canonical_key
  catalogue_row_id
  content_version                   # must match catalogue row being priced

  matchbook_event_id + market_id + runner_ids
  kalshi_event_ticker + contract_ids

  priority                          # classify_scan_lane + HOT sort key
  state                             # QUEUED | IN_FLIGHT | SUCCEEDED | RETRY_WAIT | REJECTED | DROPPED
  attempt_count
  last_attempted_at
  next_retry_at
  last_error_stage                  # order_book | get_market | fees_fx | freshness | …
  last_error_detail                 # e.g. order_book_timeout after 8s
  provider                          # matchbook | kalshi | both
```

Conceptually one item is exactly Issue #337’s tuple:

```text
fixture + canonical market key
+ Matchbook exact IDs
+ Kalshi exact IDs
```

A 26-fixture HOT roster with all four families present is at most 104 items, not
one 25s batch of 26 fixtures.

### 8.2 Item lifecycle

```text
QUEUED
  → claim one MB slot and/or one Kalshi slot as needed
  → refresh exact books (8s per call, unchanged)
  → if timeout/unavailable: RETRY_WAIT with provider-aware backoff
  → if books fresh and fees/FX known: scan_pair
  → publish decision event immediately
  → if paper-eligible and autofill ON: persist_triggered_chain immediately
  → SUCCEEDED (priced) even if not tradeable
```

`SUCCEEDED` means “this item was evaluated,” not “this item is an arb.”
Unevaluated must not be the default outcome of a sibling’s timeout.

### 8.3 Retry / backoff ownership

| Failure | Owner | Action |
| --- | --- | --- |
| `get_market_timeout after 8s` / `order_book_timeout after 8s` | that work item | `RETRY_WAIT` using existing `(2, 5, 10)` work backoff; do not leftover the roster |
| HTTP still running after wait (lease hold) | that item’s lease | slot stays occupied; **other items use remaining slots** |
| Rate limited / cooldown | provider gate | defer item; reason `deferred` / `waiting`, not venue outage |
| Structural mismatch / identity changed | catalogue | invalidate or bump `content_version`; drop this item |
| Missing fees/FX | economics prep | item not paper-eligible; publish fail-closed decision; do not retry as if it were a timeout |
| Stale quotes | freshness gate | fail closed for capture; item may be attempted next cadence |
| UNIVERSE `list_markets_timeout` | that fixture’s catalogue pass | retry fixture catalogue unit; do not mark already-ACTIVE rows exhausted |
| Hung UNIVERSE chunk | #330 watchdog | chunk ends; epoch quarantines callbacks; generation resumes; **HOT items keep running** |

Do not convert transients to `FINAL_FAILED` solely because
`paper_universe_work_max_attempts` is reached — current comment in `config.py`
already states that. Same rule for HOT items.

### 8.4 Concurrency (explicit non-goals)

Keep current caps unless a later phase **proves** provider/runtime safety:

- Matchbook 4, Kalshi 4, cluster/item in-flight bounded;
- HOT priority with starvation grants = 8;
- provider timeout 8s, discovery 15s, HOT collector envelope 25s.

The coverage win comes from **isolation**, not from more simultaneous HTTP.

A HOT cadence envelope can still exist so one worker does not run forever:
items not started this 25s remain `QUEUED` (honest), not
`scan_budget_exhausted` as if they were evaluated-and-failed.

### 8.5 What “one slow call” looks like after Phase 3

```text
HOT due, 26 fixtures × 2 catalogued families = 52 items
Kalshi slots = 4
Item 7 order_book times out at 8s
  → item 7 RETRY_WAIT next_retry_at = now+2s
  → items 8–52 still eligible
  → operator status: evaluated K / 52, 1 retry_wait, not 0/26 leftover
```

---

## 9. Economics and paper decision at item completion (Phase 4 — do not implement now)

Prepared/cacheable inputs, still fail closed:

- Matchbook cost registry / account override (`paper_account_fees` SQLite);
- Kalshi per-series/event fee metadata (may live on the catalogue/HOT leg as
  **fee** snapshot, never as a quote);
- USD/GBP FX snapshot from the FX service.

When **one** work item has complete fresh books + known economics:

1. calculate net edge;
2. publish decision immediately into current-state / watchlist projection;
3. if eligible and AUTO PAPER CAPTURE ON, call `persist_triggered_chain` immediately;
4. persist durable OPEN or explicit rejection.

Do **not** wait for the rest of the HOT/UNIVERSE batch.

### 9.1 Coordination with Issue #336 (mandatory)

Open PR #338 (`cursor/paper-eligible-auto-capture-0e43`) on this same
`owner-live` head fixes silent vanish:

- `paper_assumed_equivalent` is a non-blocking scan label but was treated as
  watchlist leftover `REJECTED`;
- `begin_paper_fill_attempt(bind_snapshot=True)` then threw `ValueError` and
  the miss was swallowed;
- no OPEN and no durable capture rejection.

Phase 4 **must**:

- reuse `persist_triggered_chain` (or the post-#336 equivalent);
- persist OPEN **or** explicit rejection (`PAPER_ENTRY_REJECTED` chain step
  already exists in `paper/chain.py`; PR #338 surfaces `PAPER_FILL_REJECTED`
  activity — unify with whatever #336 merges, do not invent a third type);
- keep freshness / treasury / complete-opening / idempotency fail-closed;
- refresh process-cached `paper_autofill_enabled` from `get_settings()` as #336 does;
- **not** reimplement watchlist leftover classification.

If #336 has not merged when Phase 4 starts, compose onto it; do not race it.

---

## 10. Observability behind the critical path (Phase 5 — do not implement now)

Move or keep as asynchronous consumers:

- append-only paper-scan audit;
- cycle/item history;
- UI aggregation (`FixtureCurrentStateStore` becomes a projection of item
  events + catalogue state);
- UNIVERSE compact checkpointing (already off-loop);
- health/read models;
- provider diagnostics.

Rules:

- No status endpoint rebuilds expensive runtime state per poll.
- `/health` stays independent of scanner load (#334).
- Item-level counters: queued / in-flight / evaluated / retry_wait / succeeded /
  rejected, **per lane**, with operation-specific health
  (`kalshi.order_book` timeout ≠ `matchbook.list_events` timeout).
- `scan_budget_exhausted` as a fixture leftover reason should disappear from
  the HOT price path. If a cadence envelope leaves items queued, say
  `not_started_this_cadence`, not exhausted.

---

## 11. Migration / compatibility (Phase 6 preview; Phase 2 must not break live data)

Preserve existing owner-live SQLite where safe:

| Store | Migration |
| --- | --- |
| `universe_generation_checkpoint` v2 | **No payload change.** Do not add books or catalogue rows. |
| Paper ledger / trades / journal | Unchanged schema for Phase 2–3. Phase 4 still writes through existing persist. |
| Watchlist | Additive fields only if needed (`work_item_id`). #336 may land first. |
| Mapping rules / review DB | Untouched. Not a runtime admission gate. |
| FX / account fees | Untouched; become catalogue-adjacent caches. |
| `FixtureCurrentStateStore` | Process memory remains a projection. After restart, ACTIVE catalogue rows rebuild HOT items; UNIVERSE rehydration of EVALUATED IDs becomes “confirm catalogue” rather than “re-scan books to recreate IDs.” |

Fallback while catalogue is empty (first boot / explicit reset):

```text
UNIVERSE behaves as today for discovery + register lookup
  → writes catalogue rows as soon as IDs are known
  → HOT items appear
  → no second matcher
```

Do not dual-run two equivalence authorities. Do not keep writing
`min_mapping_confidence` into runtime admission.

Synthetic comparison (Phase 6): same deterministic fixtures through old
`collect_and_scan` vs new UNIVERSE-catalogue + HOT-item path; assert identical
register keys and native IDs; economics compared only where both evaluated.

Live PAPER soak: Matchbook+Kalshi, execution disabled, prove:

- high % of queued work items evaluated per HOT cadence window;
- provider timeout isolated to the affected item;
- HOT cadence maintained during UNIVERSE catalogue maintenance;
- paper-eligible item capture outcome deterministic (OPEN or explicit reject);
- no real execution.

---

## 12. Exact tests required for later phases

Clock-injected; no live HTTP in CI. Owner-live soak is Phase 6, not CI.

### 12.1 Phase 2 — durable catalogue

1. UNIVERSE persist of all four keys for a synthetic MB↔K fixture with exact native IDs.
2. `TOTAL_GOALS_FT:2.5` and `TOTAL_GOALS_FT:3.5` are distinct rows; 2.5↔3.5 never ACTIVE as one key.
3. Integer/quarter totals never catalogued as `TOTAL_GOALS_FT`.
4. Extra-time / to-qualify / incomplete HDA / missing FTTS `NO_GOAL` never ACTIVE.
5. DNB/handicap/DC/team-total/player props produce no catalogue row and no Kalshi depth.
6. Register version is stored; scanner still calls `registered_canonical_key`, not confidence.
7. Disappeared Kalshi BTTS invalidates that row honestly; other families on the fixture stay ACTIVE.
8. Terminal Matchbook event marks rows TERMINAL; HOT items drop.
9. Process restart loads ACTIVE IDs without `list_markets`.
10. Compact checkpoint remains ≤256 KiB and still rejects `report` / `discovery_snapshot` keys.
11. Existing #331 / #326 / #324 four-family tests stay PASS.
12. `execution_enabled=false`; venue modules still have no place/cancel/sign.

### 12.2 Phase 3 — independent HOT work queue

1. One `order_book_timeout after 8s` fails only that work item; sibling items still evaluate in the same cadence window.
2. `remaining_soft` of a collector envelope cannot leftover-mark unstarted items as `scan_budget_exhausted`.
3. Unstarted items remain `QUEUED` / `not_started_this_cadence`.
4. Provider slot caps still 4/4; a held lease blocks one slot, not the queue.
5. HOT priority still yields to UNIVERSE after 8 consecutive grants.
6. HOT still skips `list_events` / `list_markets` when catalogue IDs exist.
7. Missing catalogue row → no silent full-event listing; `hot_relationship_missing` / confirm request.
8. Retry backoff (2, 5, 10) is per item; no thundering herd of simultaneous retries for one provider.
9. HOT can run while UNIVERSE is cataloguing (#330 overlap tests stay PASS).
10. Stale UNIVERSE chunk callbacks still cannot mutate checkpoint/catalogue (#330 epoch).
11. 26-item synthetic roster: timeout on items 0–3 does not yield 0 evaluated.
12. Existing dual-cadence / HOT targeted-refresh tests updated, not deleted.

### 12.3 Phase 4 — economics + capture at item completion

1. Decision event emitted when the item finishes, before other items in the cadence complete.
2. Autofill ON + eligible → OPEN through `persist_triggered_chain`; repeated observations idempotent.
3. Autofill ON + not eligible → durable rejection reason; no silent skip (#336 contract).
4. Autofill OFF → no OPEN; decision still published.
5. Unknown fees/FX → not eligible, fail closed, no invented 0%.
6. Quote age ≥ `paper_entry_max_quote_age_ms` → no OPEN.
7. #336 watchlist leftover / `paper_assumed_equivalent` tests stay PASS if that PR merged; if not, compose.
8. Demo/fixture replay still does not inherit live autofill.
9. Persist/auto-capture failure does not count as `scan_cycle_timeout`.

### 12.4 Phase 5 — observability

1. `/health` and `/build-info` remain dispatchable during a HOT item storm and UNIVERSE catalogue pass.
2. `/paper/live-refresh` does not call `collect_and_scan` or open a fat checkpoint.
3. Operator status shows item evaluated/retry counts independently of leftover-assembly.
4. Lane-specific + operation-specific health: HOT Kalshi order_book timeout does not display as MATCHBOOK FAILED.
5. Audit/history consumers can lag without blocking the next item.

### 12.5 Phase 6 — rollout

1. Synthetic old vs new: identical register keys and native IDs for locked fixtures.
2. Owner-live PAPER soak checklist (coverage %, timeout isolation, HOT during UNIVERSE, capture determinism, no execution).
3. Fallback: empty catalogue → UNIVERSE insert path → HOT items, no dual matcher.

---

## 13. Code paths that must **not** change in Phase 1

Phase 1 is this RFC. **No production runtime, schema, matcher, collector,
coordinator, venue, paper, or frontend behavior change.**

In particular, do not touch in this PR:

- `backend/src/sports_hedge/application/collector.py`
- `backend/src/sports_hedge/application/live_refresh.py`
- `backend/src/sports_hedge/application/scan_lanes.py`
- `backend/src/sports_hedge/application/universe_checkpoint.py`
- `backend/src/sports_hedge/persistence/universe_checkpoint.py`
- `backend/src/sports_hedge/matching/approved_register.py`
- `backend/src/sports_hedge/matching/markets.py`
- `backend/src/sports_hedge/catalogue/**`
- `backend/src/sports_hedge/application/hot_market_relationships.py`
- `backend/src/sports_hedge/application/paper_scan.py`
- `backend/src/sports_hedge/application/paper_operations.py`
- `backend/src/sports_hedge/paper/**`
- `backend/src/sports_hedge/venues/**`
- `backend/src/sports_hedge/config.py`
- `frontend/**`
- tests, except none — no test changes either

The only deliverable is this document.

### 13.1 Code paths later phases must treat as frozen contracts

Do not silently rewrite these even when implementing Phases 2–6:

| Path | Why frozen |
| --- | --- |
| `matching/approved_register.py` semantics | #331 sole runtime authority |
| `catalogue/registry.py` four-family paper flags | #326 product matrix |
| `scan_lanes.universe_chunk_wall_seconds` ignoring `next_hot_due` | Tenet 19 / #330 |
| UNIVERSE chunk epoch quarantine in `live_refresh.py` | #330 |
| Compact checkpoint v2 + 256 KiB + off-loop persist | #334 |
| Cheap `/health` / `/build-info` | #334 |
| `ReadOnlyVenue` (no place/cancel) | Tenet 02 |
| `persist_triggered_chain` capture gates | #336; compose, don’t fork |
| Fee/FX fail-closed in `paper_scan.py` | Tenet 15 |
| `paper_entry_max_quote_age_ms` fail-closed | Tenet 18 |
| Provider slot caps / 8s provider timeout **as the first lever** | Issue #337 non-negotiable |
| Runtime `min_mapping_confidence` / learned rules / mapping-review as admission | Tenet 20 |
| Dislocation burst scheduler import into HOT | Dual-cadence v1 non-goal |

---

## 14. Phased program (implementation forbidden in this PR)

| Phase | Goal | Runtime change? |
| --- | --- | --- |
| **1** | This RFC | **No** |
| 2 | Durable approved-market catalogue | Yes, additive persistence + UNIVERSE write path |
| 3 | Independent HOT work queue | Yes, replace shared leftover-as-exhaustion |
| 4 | Economics + capture at item completion | Yes, after/with #336 |
| 5 | Observability as consumers | Yes, status/item metrics; no envelope work |
| 6 | Rollout / soak / fallback | Config/migration only after 2–5 |

Each later phase is its own draft PR against then-current `owner-live`, with
Tenet 12 review, and must list whether data shown is live, historical, modelled,
or fixture/demo.

---

## 15. Explicit non-goals

- Raising `paper_scan_provider_timeout_seconds` or HTTP read timeouts as the fix.
- Broadly increasing Matchbook/Kalshi concurrency.
- Reintroducing runtime market-equivalence confidence or REVIEW_REQUIRED admission for registered rows.
- Putting exact books, discovery snapshots, or `CollectionReport` back into the UNIVERSE checkpoint.
- A second canonical identity system or a HOT-only matcher.
- Live execution, Smarkets, or expanding the paper register beyond the four families.
- Replacing #336’s persist handoff with a new capture service.
- Yielding UNIVERSE to HOT (the pre-Tenet-19 dual-cadence chunk-until-HOT model stays superseded).
- Using Research probabilities as arb admission.

---

## 16. Tenet 12 review (this RFC)

**Applicable tenets:** 02, 03, 04, 09, 10, 11, 12, 14, 15, 18, 19, 20.

**Satisfied by this design (not yet by code):**

- 02 — paper/read-only; no venue write in any proposed phase.
- 03 / 20 — register remains sole registered-row authority; catalogue stores IDs, not a new equivalence score.
- 04 / 11 — leftovers and timeouts become item-truthful; radar ≠ executable.
- 09 / 10 / 15 / 18 — capture still fail-closed on fees/FX/freshness/allocator/idempotency.
- 14 — HOT coverage can actually remain non-zero during provider slowness.
- 19 — workers stay concurrent; UNIVERSE evaluation is refined to catalogue evaluation (explicit).
- 12 — this review.

**Partial / deferred:**

- Implementation Phases 2–6.
- Tenet 19 wording that UNIVERSE “compares eligible cross-venue markets” and
  streams qualifying opportunities: refined here so UNIVERSE streams catalogue
  rows and HOT streams economics. Needs architect acceptance before Phase 2.
- #336 auto-capture vanish fix is a parallel lane; Phase 4 depends on its contract.
- Polymarket remaining on default lane venue lists until Phase 3 scheduling scope is implemented.

**Potential conflicts:**

- None accepted against paper-only, #330, #331, or #334.
- #336: no conflict if Phase 4 composes; conflict if Phase 4 forks persist.
- Tenet 19 §6–§7 evaluation language: **explicit refinement**, not a silent weaken.

**Data honesty:**

- This document is design-only.
- Timeout/budget figures are **code defaults** on `45d68d270246ec6a0b351f78562fae4244bf4d2f`.
- Owner-live 68/9/56 and HOT 0/26 figures are **operator-cited symptoms** from Issue #337, not reproduced by this RFC.
- No live books, historical odds, modelled probabilities, or demo fixtures are shown as current.

**Safety:**

- Phase 1 paper-only / read-only venue boundary preserved.
- No production code changes in this PR.

---

## 17. Recommended implementation order after architect acceptance

1. Accept this RFC, including the Tenet 19 catalogue-vs-price-engine refinement.
2. Land #336 (PR #338) on `owner-live` so capture outcomes are durable.
3. Phase 2 catalogue (additive SQLite, UNIVERSE writer, restart reader).
4. Phase 3 HOT work queue (isolation; keep 8s/4-slot caps).
5. Phase 4 item-completion economics + `persist_triggered_chain`.
6. Phase 5 item-level status; delete leftover-as-exhausted from the HOT path.
7. Phase 6 synthetic compare + owner-live PAPER soak.

Stop for architect review at each phase. Do not merge this RFC as if it were runtime.
