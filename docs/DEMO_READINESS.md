# Sports Hedge — Demo readiness

Integration branch for **Step 9** from verified `main` SHA `91da7b912a678e3c36d89c1160cfad62c8b0f71d` (merged Step 8F automatic paper entry), rebased/merged onto current `main` `0a4a47a5f66a21c4b0e21712092c00fb8ef1b446` so Core Tenet 18 is on the review branch. Downstream of parent #98 and merged 8C allocator, 8D unwind, 8E treasury, Kalshi K1, and 8F autofill.

This document is not a production-readiness claim. The owner Windows click path is `docs/DEMO_RUNBOOK.md`.

## Data honesty

| Surface | Class |
| --- | --- |
| `/paper/watchlist/tracked`, `/near`, `/triggered`, `/activity` | `LIVE PAPER` when FastAPI is reachable. Empty live lists stay empty. `demo_fixture_replay` rows are filtered out of these lists. `UNAVAILABLE` if the watchlist API is down — never back-filled with live-looking fixture arbs. **Opportunity Monitor** is the current radar board from `GET /paper/watchlist/tracked` / `FixtureCurrentStateStore` (Issue #158 merge, #168 IA). Empty current radar stays empty — not back-filled from `/paper/scans` or demo. HOT observations win for HOT fixtures; UNIVERSE observations remain for distant fixtures until sweep/TTL. Expired rows are omitted. `/near` and `/triggered` stay executable-quote-age fail-closed (~1s). |
| `/paper/collect`, `/paper/live-refresh`, venue-union fixtures | `LIVE PAPER` when collection credentials/venues respond; empty discovery stays empty. Missing Matchbook credentials stay honestly `UNAVAILABLE` for Matchbook only — Polymarket and Kalshi still collect independently. Matchbook discovery paginates `GET /edge/rest/events`. Polymarket public Gamma uses bounded per-series pagination (legacy `POLYMARKET_GAMMA_SERIES_ID` is merged into the target EPL+Championship+La Liga set). Kalshi public Trade API v2 is read-only. `/` Operations Console is the normal operator surface. `/demo` is an advanced/test fixture-replay utility and is not opened by the launcher. |
| `/demo` fixture replay | Always labelled `DEMO / FIXTURE REPLAY`. Same read-only collect path is available there for tests. Fixture replay is never substituted into live rows. |
| `/paper/scans` | `LIVE PAPER` / empty / `UNAVAILABLE`. Operations-console **Activity / scan audit history** is the latest 100 **audit** observations (`GET /paper/scans?limit=100`), newest `scanned_at` first — not radar current-state. Collapsed/secondary after Opportunity Monitor and Open paper positions. Age is derived from each audit row's `scanned_at`. Client sorting is this loaded window only. See `docs/PAPER_SCAN_AUDIT_WINDOW.md`. |
| `/paper/treasury` | Authoritative **persistent paper treasury** (8E), not a labelled demo-only pool widget. Three native venue books. GBP carrying values are FX translations, not spendable cash. |
| `/paper/liquidity-pools` | Paper config / standing capital used by the solver. Aligned to treasury seed amounts on demo reset. Not a second source of truth for locks. |
| `/paper/demo/walkthrough`, `/paper/demo/reset`, `/paper/demo/fixture-replay` | Operator demo orchestration. Reset/start is live paper treasury. Fixture replay is always `DEMO / FIXTURE REPLAY`. |
| `/paper/simulate-fill` + 8F autofill | `LIVE PAPER` against persisted scan state when a solver-triggered opportunity exists; `FIXTURE_DEMO` when the labelled replay path sets that provenance. Postings are simulated cash locks, not venue fills. |
| Matchbook / Kalshi paper fills | `INTERNAL_SIMULATED`. Kalshi is first-class INTERNAL, not MANUAL_EXTERNAL. |
| Polymarket demo autofill | `PAPER_SIMULATED_EXTERNAL` — a paper-only stand-in. Distinct from operator-recorded `MANUAL_EXTERNAL`. Never presented as a real provider execution. |
| `POST /paper/trades/{id}/close-plan` | `MODELLED` 8D hold-vs-unwind. Analytical only. Conditionally releasable is not spendable. |
| `POST /paper/trades/{id}/unwind` and `/settle` | Paper close paths that may post 8E release after a complete validated unwind or explicit settlement. |
| Research Home, Matchday, Team Explorer, Scenario Lab/Planner | `DEMO / FIXTURE` (SRC, quotes, fees, EV) |
| `/research/historical/coverage` | `REAL HISTORICAL` when SQLite facts/odds files contain rows; otherwise `UNAVAILABLE` |
| Tenet 17 analogue / comparable-move model | `UNAVAILABLE` |
| Windows one-click launcher | Local process helper. Not a hosted/Vercel deployment. Start sets `PAPER_AUTOFILL_ENABLED=true` (AUTO PAPER CAPTURE ON for qualifying `LIVE_PAPER` only; allocator-sized; no venue orders), `PAPER_LIVE_REFRESH_ENABLED=true`, and `ACCOUNTING_SCHEDULE_ENABLED=true` for that process only (application defaults remain false). Labelled `/demo` replay does not inherit autofill. The FX scheduler bootstraps the latest published ECB USD close on a fresh DB, including weekend carry-forward. Stop verifies PID command/path identity before kill. |

Unknown costs still fail closed. Native GBP and the two USD venue pools are never summed. Polymarket USD and Kalshi USD remain distinct.

## What is genuinely wired (current main + Step 9)

- Persistent 8E paper treasury with three native pools: Matchbook GBP; Polymarket USD; Kalshi USD. Demo seed is £1,000 Matchbook and a USD amount representing £1,000 carrying value at the explicit demo FX snapshot (default 0.80 GBP/USD, source `paper_demo_fx_snapshot`).
- Ordinary treasury/demo reset **fails closed** while open locks or open trades exist. The Step 9 **reinitialize store** path is an explicit destructive demo reset: remaining locks return at zero betting P&L (`source=paper_demo_reset`), open trades are abandoned as `demo_reset` (not a market result), identities are archived, and the three pools are reseeded.
- Kalshi is first-class INTERNAL (data + paper enabled, execution disabled). Pairwise paper scans include Matchbook↔Polymarket, Matchbook↔Kalshi and Polymarket↔Kalshi without requiring Matchbook for the last.
- Step 8F automatic paper entry: allocator recommended stakes are authoritative. OPEN only after a complete validated hedge and exact treasury locks. Repeated refresh is idempotent.
- `PAPER_SIMULATED_EXTERNAL` vs `MANUAL_EXTERNAL` stays distinct. Demo/8F autofill may simulate the Polymarket external leg in paper; it does not record a real MANUAL_EXTERNAL confirmation.
- 8D hold-vs-unwind evaluates executable reverse-side economics. Spread convergence is not a close trigger. Clock / modelled time-to-release is advisory (`settles_or_releases_capital=false`) and never makes capital spendable.
- Two authoritative release paths: validated paper unwind that posts 8E, or explicit paper settlement that posts 8E. Kalshi SELL close fees are not modelled; unwind involving Kalshi fails closed rather than inventing a fee. Settlement remains the Kalshi close path.
- Labelled `DEMO / FIXTURE REPLAY` exercises the same allocator → autofill → treasury → unwind/settlement lifecycle and is never mixed into empty live watchlists.
- Windows double-click start/stop launchers under `scripts/windows/`. Hidden local processes, health wait, duplicate-process avoidance, file logs, visible startup error. `PAPER_AUTOFILL_ENABLED=true` so qualifying **LIVE_PAPER** decisions auto-capture through `persist_triggered_chain` (allocator-sized, fail-closed, idempotent across HOT cycles); labelled `/demo` fixture replay does **not** inherit that flag and remains qualify → £10 preview/confirm. `PAPER_LIVE_REFRESH_ENABLED=true` and `ACCOUNTING_SCHEDULE_ENABLED=true` for that local demo process only; application defaults remain false. Fresh FX DB bootstraps the latest published ECB USD close (weekend/holiday carry-forward) and does not use treasury demo FX for arb qualification. Stop refuses to kill a reused PID unless command/path matches the launcher identity. The launcher opens `/` Operations Console. `/demo` remains a labelled DEMO / FIXTURE REPLAY utility and can still run `/paper/collect` plus **Refresh Live Discovery** for lifecycle acceptance when no live arb exists. Exact Windows click path: `docs/DEMO_RUNBOOK.md`. No Vercel/cloud migration.

## What remains fixture/demo / unavailable

- Research value quotes, SRC tables, featured Arsenal v Fulham 12:30 story.
- Priority Alert demo ticket `pa-ncl-ars-2026-04-12-mr` and localStorage MANUAL_EXTERNAL confirmation UI.
- Full Tenet 17 analogue retrieval.
- Authorised live venue fee snapshots replacing demo Research fee assumptions. Arb scan uses `VenueCostSnapshot` where the per-quote rule exists; unsupported scopes fail closed.
- Live qualifying arbitrage during a review session is not guaranteed. Use labelled fixture replay rather than fabricating a live strike.
- Kalshi SELL/close fee model (unwind fail-closed).
- Hosted/cloud operator console.

## Exact Step 9 operator walkthrough

1. **Reset / start** — double-click `scripts/windows/Start-SportsHedge-Demo.bat` then `/` Operations Console. PAPER MODE / NO EXECUTION. Seed or reinitialize the three separated native pools from Paper Treasury. Ordinary reset refuses destruction while locks/trades are open.
2. **Discovery / tracking** — `/` operations console. One-click launch opens `/`. Tracked / near / triggered stay honestly empty when empty. Near is not relabelled as arbitrage. Missing credentials/providers fail as UNAVAILABLE for that venue; other venues still collect. Fixture replay is never mixed into live rows.
3. **Live paper auto-capture** — when the Windows launcher enables `PAPER_AUTOFILL_ENABLED`, a decision that already passes canonical equivalence, solver arbitrage, fees/FX, depth/liquidity/risk, quote freshness, and allocator acceptance opens once through the existing 8F path (`LIVE_PAPER` provenance, native locks, Paper Portfolio). Tracked/Near or a gross price is not enough. Repeated HOT observations are idempotent. Historical discovery (including the Leeds v Newcastle 1.35% net candidate) is not backfilled into a trade. Labelled `/demo` **Qualify labelled replay** then £10 prepare/confirm remains the explicit replay path and does not inherit live auto-capture. Matchbook/Kalshi INTERNAL; Polymarket demo `PAPER_SIMULATED_EXTERNAL`. OPEN only after complete hedge + 8E locks.
4. **Active position / capital** — `/` plus `/paper` and `/treasury`. Opportunity + solver model, venue legs, native stake, fill kind, guaranteed opening economics when proven, available vs locked native capital by venue/currency, modelled time-to-release basis/confidence when present. PAPER MODE visible.
5. **Hold vs clean unwind** — 8D close-plan on the open trade using current reverse-side read-only economics (fixture replay supplies labelled reverse quotes). Hold-to-settlement P&L vs validated exit P&L, unwind cost, capital releasable only if the full close fills. Advisory remaining lock / opportunity-cost context is not spendable.
6. **Close lifecycle** — validated paper unwind (when fully executable) **or** explicit paper settlement, both posting 8E release. After close: realised betting P&L, fees, native cash released/remaining, final native balances by venue/currency, GBP carrying values (not native cash), append-only journal/audit.
7. **No-live-arb fallback** — if no live qualifying arb exists, labelled `/demo` **DEMO / FIXTURE REPLAY** runs the same lifecycle. It is never substituted into an empty live list without that label.

## Core tenets (PASS / PARTIAL / FAIL)

| Tenet | Result | Evidence |
| --- | --- | --- |
| 01 Product structure | **PASS** | Separate Arbitrage / Research nav. Research never labelled guaranteed arb. `/` is the only normal operator surface; `/demo` is an advanced labelled fixture-replay utility, not a competing console. |
| 02 Paper mode | **PASS** | `execution_enabled=false`. No place/cancel/sign/wallet/trading-auth/write API. Demo launcher cannot turn execution on. |
| 03 Canonical equivalence | **PARTIAL** | Shared identity and incomplete-fingerprint fail-closed remain. Live discovery still bounded. |
| 04 Arbitrage operations | **PASS** (paper) | Tracked + near + triggered; 8F OPEN-after-locks; 8D/8E close paths; near ≠ triggered. |
| 05 Research & value | **PASS** (demo quotes) | Unchanged Research demo surfaces. |
| 06 Scenario response | **PASS** (demo) | Unchanged. |
| 07 Manager / regime | **PASS** (demo) | Unchanged. |
| 08 Historical provenance | **PASS** (coverage seam) | Repository-derived counts; missing files → UNAVAILABLE. |
| 09 Liquidity / capital / priority alerts | **PASS** (paper) | Authoritative 8E native pools; allocator sizes 8F; unwind/clock never spendable; demo ticket still distinct. |
| 10 Accounting / FX / books | **PASS** (paper subledger) | Append-only journal + 8E postings; GBP carrying ≠ native cash; two USD venues not commingled. Not a production GL. |
| 11 UI / data honesty | **PASS** | Live vs DEMO / FIXTURE REPLAY labelled; empty live lists not substituted. Dual-cadence Fast/Full scan copy is **accepted design** (#158), not shipped. |
| 12 Agent review | **PASS** | This document + PR tenet list. |
| 13 Event intelligence | **PARTIAL** | Existing MI/trends; no invented live scores. |
| 14 Event-driven dislocation arb | **PARTIAL** | Burst scanner on main; UI does not treat dislocation as arb. Dual-cadence HOT 25s/30s + chunked UNIVERSE 150s/180s is **accepted design** (#158), not shipped. |
| 15 Effective venue economics | **PARTIAL** | Arb/demo scan uses `VenueCostSnapshot`. Kalshi SELL close unknown → fail closed. Research still uses typed demo snapshots. |
| 16 External manual legs | **PASS** (paper distinction) | `PAPER_SIMULATED_EXTERNAL` ≠ `MANUAL_EXTERNAL`. No VPN/geo bypass. |
| 17 Historical market movement | **PARTIAL** | Coverage counts exposed; analogue model UNAVAILABLE. |
| 18 Execution atomicity | **PARTIAL** | Applicable. Realistic paper fills may model latency/slippage/depth/partials, but paper full-fill success does **not** prove simultaneous real fills. Production execution remains a later shadow/micro-live gate. `execution_enabled=false`; this step adds no live order path. |

### Conflicts / non-weakening

No tenet was silently weakened to make the demo “work”. Kalshi unwind stays fail-closed instead of inventing SELL fees. Fixture replay is not injected into empty live watchlists. Phase 1 collection remains read-only. Clock estimates do not release capital. Paper full-fill success is not treated as proof of simultaneous real fills (Tenet 18).

## Tracked current-state contract

### Current (Issue #158) — per-identity radar merge

`GET /paper/watchlist/tracked` is the **current radar board** from `FixtureCurrentStateStore`, not the paper decisions of a single latest `CollectionReport`. Operations-console **Opportunity Monitor** (#168) is the operator-facing current-radar table for that same read model. Append-only `/paper/scans` remains a secondary audit window.

- Empty until a live collection completes. Persisted history/activity may exist; the board stays empty.
- HOT observations win for fixtures currently in the HOT cohort (in-play, ≤60m pre-kickoff, or kickoff-passed unknown in-play within 3h).
- UNIVERSE observations remain for distant fixtures until the next sweep or radar TTL (HOT 90s / UNIVERSE 360s).
- An empty HOT cycle does not clear in-TTL UNIVERSE rows.
- Partial UNIVERSE leftovers do not clobber a previous valid in-TTL evaluation.
- Expired observations are omitted (fail closed). They must not look current.
- Qualifying / TRIGGERED opportunities from either lane persist in that cycle (no lane delay).
- `Near` / `Triggered` / paper entry stay fail-closed on executable quote age (`max_quote_age_ms`, default 1000ms). `radar_current` is never BET-actionable.
- Operator UI exposes **Fast scan** and **Full sweep** separately. `interval_seconds` / `last_completed_at` remain HOT aliases.

This contract is enforced by `backend/tests/test_tracked_current_snapshot.py` and `backend/tests/test_dual_cadence_scheduler.py`.

### Previous (as of #131 / #157) — latest completed cohort (superseded)

Tracked was previously only the latest completed collection's paper decisions. Dual cadence cannot keep that rule without hiding distant fixtures after a HOT pass or mixing stale rows into the current board. The replacement is above. Full rules: `docs/DUAL_CADENCE_SCANNER.md` §6.

## Safety

`SPORTS_HEDGE_MODE=paper` / `SPORTS_HEDGE_EXECUTION_ENABLED=false` remains the Phase 1 boundary. This pass adds no venue order methods, wallet signing, trading auth, VPN/proxy, or geo-circumvention, does not migrate to Vercel/cloud, and does not claim production readiness.
