# Sports Hedge — Demo readiness

Integration branch: `demo/tomorrow-integration`.

This is a **demo convergence** of reviewed UI work. It does **not** merge blocked backend architecture into `main`. Phase 1 remains paper-only.

## Data honesty

| Surface | Class |
| --- | --- |
| Paper scan history / live near-arb / paper-eligible triggers | `PERSISTED_PAPER` / `LIVE` when FastAPI is reachable; otherwise empty or explicitly labelled `UNAVAILABLE` / `DEMO / FIXTURE` |
| Near-Arb demo cards, liquidity pools, capital P&L, activity fill lifecycle, Priority Alerts, MANUAL_EXTERNAL workflow | `DEMO / FIXTURE` |
| Research Home, Matchday, Team Explorer, Scenario Lab, Scenario Planner | `DEMO / FIXTURE` (SRC, quotes, fees, EV) |
| Historical repository seam | `UNAVAILABLE` (PRs #35 / #36 not merged) |
| Market Intelligence / Trends | existing dashboard fixtures / MI API when available — not a new live warehouse |

Fee snapshots used for Research ranking are **typed demo assumptions**. Unknown costs fail closed (`MISSING_COSTS`); they are never treated as 0%. Quote age is typed on each demo quote. **DEMO ASSUMPTION:** max age for `VALUE` is **10 minutes**; older quotes become `STALE_QUOTE` and cannot remain ranked `VALUE`.

## What is genuinely wired

- Existing FastAPI paper-scan read model on `/` (scan list, eligibility, net edge, rejection reasons).
- Near-Arb watchlist from live scans only when the **only** remaining rejection is `net_edge_below_threshold` (or equivalent threshold-only state). Stale/missing-fee/FX/settlement/depth failures are **not** shown as near-arbs.
- Paper scan control remains read-only collection + paper simulation.
- Scenario Planner persistence is browser `localStorage` (explicit seam).
- Priority Alert operator state is localStorage.
- External-leg paper confirmation records (price, size, currency, timestamp, reference) are localStorage. Status without economics is not treated as confirmed.

## What remains fixture/demo

- Research value quotes, SRC tables, manager-era splits, featured Arsenal v Fulham 12:30 story.
- Native pools (Matchbook GBP, Smarkets GBP, Polymarket USD) and GBP carrying values.
- Priority Arb Alert tickets, fill-confidence scores, MANUAL_EXTERNAL confirmation form.
- Event-driven burst scanner, near-arb backend service, notification routing, scenario value engine: **not merged** (architecture review still blocking).

## Historical data

PRs #35 and #36 stay out of this branch. Provenance overwrite, naive-UTC coercion, alias fail-open, and split identity (`hist:` vs hashed `match:`) are unresolved. The Research Home shows an `UNAVAILABLE` coverage seam instead of fabricating live history.

## Outstanding blockers (next sprint)

1. Shared canonical identity + append-only provenance for historical stats and odds (#35, #36).
2. Fee-basis / side / order-role snapshot in the Scenario Value Engine (#52) so Research and Arbitrage share one production economics layer.
3. Near-arb tracker: unknown currency / unknown quote age must fail closed (#61).
4. Priority alerts + notifications: one alert contract, `MANUAL_EXTERNAL` in the backend, deep-link `/arbitrage/priority-alerts/{id}` (#60, #58, #59).
5. Event-driven burst scanner: no naive-UTC, no negative quote-age clamp (#66).
6. Strategy-book `capital_source` dimension (#56).
7. Replace demo fee assumptions with authorised venue fee snapshots (Core Tenet 15).

## Exact demo walkthrough

Assume Saturday 12 September 2026, ~11:30 BST, **Arsenal v Fulham at 12:30 BST**. That fixture is shared across Research Home, Matchday, Team Explorer and the planner preview.

1. `/` — Arbitrage operations console. PAPER MODE / NO EXECUTION. Near-Arb vs triggered paper-eligible vs MANUAL_EXTERNAL are separate sections. Open Priority Alerts seam.
2. `/arbitrage/priority-alerts` then `/arbitrage/priority-alerts/pa-ncl-ars-2026-04-12-mr` — exceptional paper ticket, native pools, PREPARE MANUAL TICKET (not PLACE BET), PROCEED WITH EXTERNAL COUNTERPARTY. Confirming requires executed price, size, timestamp and external reference; the saved paper record is rendered back. Hedge revalidation is labelled NOT PERFORMED.
3. `/research` — odds-weighted value table. Rank by **net** odds. Smarkets higher headline can lose after fees. High-SRC Arsenal match-result row is `NO_VALUE`. Quotes older than 10m (demo assumption) are `STALE_QUOTE`.
4. Click Arsenal v Fulham → `/matchday` featured card — same opponent/kickoff/managers; SRC plus value overlay.
5. `/teams` → `/teams/arsenal` — Arteta era, Fulham 12:30, SRC table, value overlays, Scenario Lab links.
6. `/scenario-lab?team=arsenal&scenario=favourite-concedes-first&metric=corners&window=0-15` — SRC matrix, era split, value overlay (`VALUE` vs `NO_VALUE`). Unknown `window` query values are not coerced to `0-15`.
7. `/scenario-planner` — paper rule with SRC **and** EV/freshness/known-fees gates. High-SRC Fulham snapshot can fail `NO_VALUE`.
8. `/treasury` — three native pools, never summed.
9. `/paper` — existing paper portfolio.

## Core tenets (PASS / PARTIAL / FAIL)

| Tenet | Result | Evidence |
| --- | --- | --- |
| 01 Product structure | **PASS** | Separate Arbitrage / Research nav and copy. Research never labelled guaranteed arb. |
| 02 Paper mode | **PASS** | No place/cancel/wallet path. External workflow is paper confirmation only. |
| 03 Canonical equivalence | **PARTIAL** | Shared frontend IDs; backend historical identity still blocked. Polymarket omitted where settlement is not equivalent. |
| 04 Arbitrage operations | **PARTIAL** | Console + near-arb filter + lifecycle language. Full depth/fee solver still the existing paper scan, not the blocked watchlist PR. |
| 05 Research & value | **PASS** (demo) | Ranked on net EV; high SRC can be `NO_VALUE`; N/confidence/quality shown. Quote age is gated: demo max 10m → `STALE_QUOTE`. |
| 06 Scenario response | **PASS** (demo) | Team vs league, windows, SRC, sample. |
| 07 Manager / regime | **PASS** (demo) | Arteta vs Silva / era splits / mix warnings. |
| 08 Historical provenance | **PARTIAL** | Seam labelled UNAVAILABLE; blocked PRs not merged. |
| 09 Liquidity / priority alerts | **PASS** (demo UI) | Native pools; AUTO_POOL / MANUAL_OVERRIDE / MANUAL_EXTERNAL distinct. Backend alerts not merged. |
| 10 Accounting / FX / books | **PARTIAL** | Native vs GBP carrying labels; no audited ledger on this branch. |
| 11 UI / data honesty | **PASS** | Demo/live/unavailable labelled; live near-arb no longer silently replaced with fixtures when the API is up. |
| 12 Agent review | **PASS** | This document. |
| 13 Event intelligence | **PARTIAL** | Existing MI/trends only; no new causality claims. |
| 14 Event-driven dislocation arb | **PARTIAL** | UI language + priority-alert path; burst scanner not merged. |
| 15 Effective venue economics | **PARTIAL** | Demo fee snapshots + net ranking + unknown→MISSING_COSTS. Production fee engine still outstanding. |
| 16 External manual legs | **PARTIAL** | Demo UI: `MANUAL_EXTERNAL` ≠ `AUTO_POOL`; proceed ≠ place; confirmation requires typed executed price/size/currency/timestamp/reference and renders the paper record. Remaining hedge is labelled **not revalidated**. No expiry engine, no backend `EXTERNAL_LEG_*` persistence, no live fill. No VPN/geo bypass. |

### Conflicts / non-weakening

No tenet was silently weakened. Blocked backend PRs were **not** merged to make the demo look live.

## Safety

`SPORTS_HEDGE_MODE=paper` / execution disabled remains the Phase 1 boundary. This branch adds no venue order methods, VPN/proxy, or geo-circumvention.
