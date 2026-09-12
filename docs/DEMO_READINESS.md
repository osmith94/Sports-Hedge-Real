# Sports Hedge — Demo readiness

Integration branch for **Step 9** from `main` SHA `d8474b4edc63c1d7f5b4fc16dff31546ca4a1985` (Step 8 five-season PL/Championship backfill).

This is an operator-usability / demo-readiness pass. It does **not** add a new product area. Phase 1 remains paper-only. This document is not a production-readiness claim.

## Data honesty

| Surface | Class |
| --- | --- |
| `/paper/watchlist/tracked`, `/near`, `/triggered`, `/activity` | `LIVE PAPER` when FastAPI is reachable; empty live lists stay empty; `UNAVAILABLE` if the watchlist API is down (near/triggered may then show labelled `DEMO / FIXTURE`) |
| `/paper/collect`, `/paper/live-refresh`, Matchbook-discovered fixtures | `LIVE PAPER` when collection credentials/venues respond; empty discovery stays empty; scores `UNAVAILABLE` unless Matchbook payload includes them. Missing Matchbook credentials stay honestly `UNAVAILABLE` / HTTP 503 — never a faked login. Live Matchbook discovery resolves the football sport id from read-only `GET /edge/rest/lookups/sports`, queries `GET /edge/rest/events` with that `sport-ids` filter plus a bounded live/near-future `after`/`before` window, and paginates with `offset` until exhausted or a safety page cap (explicit `matchbook_discovery` issue). Collector allowlisting to English Premier League, EFL Championship and Spain La Liga remains the correctness gate. Polymarket public Gamma discovery uses documented `GET /sports` series IDs (EPL `10188`, EFL Championship `10355`, La Liga `10193`) with bounded per-series pagination. Unmatched rows distinguish `event_identity_mismatch`, `series_not_queried` (legacy EPL-only override), and genuine `unmatched / no supported Polymarket coverage`. |
| Paper scan history `/paper/scans` | `LIVE PAPER` / empty / `UNAVAILABLE` |
| Operator comfort-threshold control | UI comparison only against backend `current_net_edge`; does not mutate lifecycle |
| Priority Alerts `/priority-alerts` | `LIVE PAPER` when the API is up; empty live list stays empty. Walkthrough ticket `pa-ncl-ars-2026-04-12-mr` is `DEMO / FIXTURE` |
| MANUAL_EXTERNAL confirmation (localStorage UI) | `DEMO / FIXTURE` paper workflow. Backend `POST /paper/simulate-fill` + remaining-hedge revalidation is `LIVE PAPER` against persisted scan state, or `FIXTURE_DEMO` when tests/operator label it so |
| `/paper/simulate-fill` + paper journal | `LIVE PAPER` when a solver-triggered opportunity and fill plan exist; otherwise empty/409. Postings are simulated cash locks, not venue fills. Native GBP and USD stay separate |
| Native liquidity pools / treasury balances | `DEMO / FIXTURE` on `/treasury`. Paper journal GBP presentation is functional reporting over simulated locks, not operator-visible audited GL |
| Research Home value table, Matchday, Team Explorer, Scenario Lab, Scenario Planner | `DEMO / FIXTURE` (SRC, quotes, fees, EV) |
| `/research/historical/coverage` | `REAL HISTORICAL` when SQLite facts/odds files contain rows; otherwise `UNAVAILABLE` |
| Tenet 17 analogue / comparable-move model | `UNAVAILABLE` |
| Market Intelligence / Trends | existing MI API when available; not a warehouse analogue model |

Unknown costs still fail closed. Native GBP and USD pools are never summed.

## What is genuinely wired

- Canonical paper watchlist: tracked markets, near-arbs, triggered opportunities, activity.
- Repeated **read-only** Matchbook → Polymarket collection: Matchbook is the primary live fixture-discovery source; Polymarket is matched onto the same canonical event. Console auto-refresh (default 30s, minimum 15s) re-runs `/paper/collect` while the Arbitrage page is open. Optional server loop via `PAPER_LIVE_REFRESH_ENABLED` (off by default so CI does not call venues).
- Append-only watchlist observation history so current net margin and distance-to-strike move as prices refresh. Strike narrative is **observed sequence only** (approaching / moving away / stable), never causation.
- Source, last updated and quote age are shown on tracked/near rows. Quote age is conservative (oldest required venue clock). Missing, invalid or future required timestamps stay unknown rather than clamping to zero. Near/triggered reads re-age persisted observations against the evaluation clock; after refresh stops, stale/unknown rows leave those lists and remain on tracked as rejected history, not current guaranteed economics. Matchbook age is a real source clock or an explicit retrieval-age basis — never an invented provider timestamp.
- Matchbook fixture `status` / in-running flag preserved when present. Live scores only if Matchbook payload includes explicit home/away score fields; otherwise labelled unavailable. No third-party live-score provider.
- Backend-calculated `current_net_edge`, `trigger_net_edge`, `distance_to_trigger_pp`, and optional `gross_edge`.
- Negative net margin remains visible as below break-even.
- When gross complete-set prices are effectively equal and net margin is negative, the UI names fees/costs as the reason.
- Paper scan `net_edge` is persisted from implied probability even when the solution is not an arb (guaranteed profit remains solver-owned / triggered-only).
- Paper collection remains read-only + paper simulation. The two-process localhost console may call the paper JSON API only from the configured CORS allowlist (default `http://localhost:3000` / `http://127.0.0.1:3000`); other origins are denied. This is not a trading permission.
- Historical coverage counts are SQL/repository-derived, not hardcoded 4,660 / 393,064 figures.
- Priority Alert backend list is read when reachable; demo MANUAL_EXTERNAL walkthrough stays labelled demo. Solver-validated TRIGGERED scans can ingest the existing Priority Alert service (qualification still applies; Polymarket legs stay `EXTERNAL_OPERATOR` / MANUAL_EXTERNAL).
- Arb scan economics use `VenueCostSnapshot` / `apply_venue_costs` (per-quote supported bases only). Legacy `FeeSnapshot` haircuts cannot produce a strike. Incomplete settlement fingerprints fail closed even when unknown fields match. Configured FX spread and book slippage are labelled assumptions; missing required costs/FX fail closed.
- Explicit PAPER-ONLY `POST /paper/simulate-fill` walks `PaperFillSimulator`, writes watchlist `PAPER_FILLING` / `PARTIAL` / `FILLED`, and posts balanced append-only paper journal locks. MANUAL_EXTERNAL confirmation is operator-recorded realised exposure; Sports Hedge does not simulate that it placed that leg. Remaining hedge is revalidated before any subsequent internal paper fill.

## What remains fixture/demo / unavailable

- Research value quotes, SRC tables, featured Arsenal v Fulham 12:30 story.
- Native pool balances and GBP carrying values.
- Priority Alert demo ticket and localStorage external-leg confirmation.
- Full Tenet 17 analogue retrieval (N, regime, weak/no relationship, no precedent) — seam preserved, model not built.
- Authorised live venue fee snapshots replacing demo Research fee assumptions.

## Historical data

Facts (#35) and odds (#36) plus Step 8 PL/Championship backfill are on `main`. The Research Home coverage panel reads `/research/historical/coverage`:

- match count from the facts repository;
- stored observation count from the odds repository;
- same-line opening→closing pairs (equivalent proposition/line);
- Asian handicap line shifts (structural, not pure price movement).

If the SQLite files are absent in an environment, the UI says `UNAVAILABLE` rather than inventing warehouse coverage.

## Outstanding blockers (next sprint)

1. Wire live Priority Alert tickets into the same UI contract as the demo walkthrough without mixing DEMO rows into empty live lists.
2. Replace demo Research fee assumptions with authorised venue fee snapshots (Tenet 15 Research path). Arb scan now uses `VenueCostSnapshot` where the per-quote rule exists; MARKET_NET / ACCOUNT_PERIOD / FORMULA-without-rule still fail closed.
3. Build the Tenet 17 analogue read API (sample size, quality, weak/no relationship, no precedent) without treating correlation as causation.
4. Operator-visible treasury/full GL instead of the smallest paper journal seam.
5. Polymarket Gamma coverage for the three demo-target series is bounded-paginated; other sports/leagues remain out of scope. Empty Championship/La Liga series results stay unmatched.
6. Do not claim production readiness.

## Exact operator walkthrough

Even when there is **no current arbitrage strike**, a reviewer can still understand the flow.

1. `/` — Arbitrage operations console. PAPER MODE / NO EXECUTION.
   - **Matchbook fixture discovery**: latest read-only collection; Polymarket matched or unmatched; no invented scores.
   - **Tracked markets**: canonical markets, current net margin (including negative), backend trigger, distance to trigger, source/last updated/quote age, operator comfort comparison (`0.5% / 1.0% / 2.0%` plus a distinct backend trigger), observed strike narrative.
   - Auto-refresh re-runs the existing `/paper/collect` path while the console is open.
   - **Near-Arb**: WATCHING/APPROACHING validated watch candidates below trigger; not guaranteed arb.
   - **Triggered**: empty live state stays empty; guaranteed profit only if solver-triggered.
   - **Priority Alerts** seam: live count (possibly 0) plus labelled DEMO walkthrough.
   - **MANUAL_EXTERNAL** paper confirmation form.
2. `/arbitrage/priority-alerts` then `/arbitrage/priority-alerts/pa-ncl-ars-2026-04-12-mr` — DEMO exceptional paper ticket. PREPARE MANUAL TICKET is not PLACE BET. PROCEED WITH EXTERNAL COUNTERPARTY requires executed price, size, timestamp and reference.
3. `/research` — odds-weighted **DEMO** value table, plus **REAL HISTORICAL** coverage counts when the API can open the warehouse files. Analogue model UNAVAILABLE.
4. `/matchday` — featured Arsenal v Fulham 12:30 **DEMO**.
5. `/teams` → `/teams/arsenal` — Arteta-era **DEMO** SRC/value overlays.
6. `/scenario-lab?team=arsenal&scenario=favourite-concedes-first&metric=corners&window=0-15` — SRC matrix **DEMO**.
7. `/scenario-planner` — paper rule with SRC and EV gates **DEMO** / localStorage.
8. `/treasury` — three native pools, never summed, **DEMO**.
9. `/paper` — existing paper portfolio.

## Core tenets (PASS / PARTIAL / FAIL)

| Tenet | Result | Evidence |
| --- | --- | --- |
| 01 Product structure | **PASS** | Separate Arbitrage / Research nav. Research never labelled guaranteed arb. |
| 02 Paper mode | **PASS** | No place/cancel/wallet path. Paper fill and MANUAL_EXTERNAL stay paper. |
| 03 Canonical equivalence | **PARTIAL** | Shared frontend IDs; incomplete settlement fingerprints cannot match; Yes/No is not loosened to 1X2; historical identity now shared `match:sha256` on main. Live discovery still first-page Gamma. |
| 04 Arbitrage operations | **PASS** (paper) | Tracked + near + triggered + activity; net edge after VenueCostSnapshot; explicit simulated fill + journal; near ≠ triggered. |
| 05 Research & value | **PASS** (demo quotes) | Ranked on net EV; high SRC can be `NO_VALUE`. |
| 06 Scenario response | **PASS** (demo) | Team vs league, windows, SRC, sample. |
| 07 Manager / regime | **PASS** (demo) | Arteta vs Silva / era splits. |
| 08 Historical provenance | **PASS** (coverage seam) | Repository-derived counts; missing files → UNAVAILABLE. Excel is not the truth store. |
| 09 Liquidity / priority alerts | **PARTIAL** | Native pools labelled DEMO on treasury; TRIGGERED scans can ingest PriorityAlertService; demo ticket distinct; MANUAL_EXTERNAL ≠ AUTO_POOL. |
| 10 Accounting / FX / books | **PARTIAL** | Append-only paper journal proves balanced GBP postings and native separation; not a full GL. FX spread labelled. |
| 11 UI / data honesty | **PASS** | Live/historical/modelled/demo/unavailable labelled; empty live watchlists not substituted; scores not fabricated. |
| 12 Agent review | **PASS** | This document. |
| 13 Event intelligence | **PARTIAL** | Existing MI/trends; Matchbook in-running/status only if present; no live-score provider; strike narrative is not causation. |
| 14 Event-driven dislocation arb | **PARTIAL** | Burst scanner is on main; UI still does not treat dislocation as arb. |
| 15 Effective venue economics | **PARTIAL** | Arb scan uses shared `VenueCostSnapshot` / `apply_venue_costs`. Unsupported scopes fail closed. Research still uses typed demo snapshots. Dashboard 0% is `assumed_zero`. |
| 16 External manual legs | **PARTIAL** | Backend remaining-hedge revalidation before paper fill; demo UI confirmation remains labelled demo. No VPN/geo bypass. |
| 17 Historical market movement | **PARTIAL** | Same-line vs AH line-shift counts exposed; analogue model UNAVAILABLE; no causation language. |

### Conflicts / non-weakening

No tenet was silently weakened. Comfort-threshold math is a comparison of backend `current_net_edge` against a selected threshold; it does not recompute venue fees in the browser or change watchlist status. Strike narrative is observational. Phase 1 collection remains read-only.

## Safety

`SPORTS_HEDGE_MODE=paper` / execution disabled remains the Phase 1 boundary. This pass adds no venue order methods, wallet signing, trading auth, VPN/proxy, or geo-circumvention, and does not claim production readiness.
