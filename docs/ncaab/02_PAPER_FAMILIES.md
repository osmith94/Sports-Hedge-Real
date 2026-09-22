# NCAAB Phase 1 — PAPER families (blocked)

**Issue:** #508  
**Evidence:** `docs/ncaab/01_PROVIDER_EVIDENCE_CENSUS.md`  
**Mode:** PAPER MODE · EXECUTION DISABLED · GET / read-only only  
**Data class:** LIVE captured public payloads from 2026-09-22, plus structural synthetic markets used only in tests. Not modelled probabilities. Not demo soccer or NFL data.

This layer adds a dedicated NCAA Division I men's basketball module that feeds the existing UNIVERSE → BACKGROUND → HOT scanner. Soccer aliases, NFL recognisers, and 1X2 register rows are not reused. No NCAAB-specific fee tables. Provider concurrency remains Matchbook 4 / Polymarket 8 / Kalshi 4.

---

## Selector

| Field | Value |
|---|---|
| Canonical code | `ncaab` |
| Display name | NCAA Men's Basketball |
| Group | College Basketball |
| Selector label | NCAA Men |
| Default selected | no |
| Selectable with zero fixtures | **yes** |

A healthy off-season state is **0 fixtures**, not a hidden competition. NCAAW is not selectable in this lane.

Division I men's registry: **362** programs from the 2026-09-22 ESPN public roster census. Generic labels (`Miami`, `Loyola`, `State`, `UT`, `SC`, …) fail closed. Learned aliases cannot bypass curated conflicts.

---

## Structural families (normalisation only)

Exactly three full-game families are recognised structurally:

| Family | Operator label | Canonical key | Line rule |
|---|---|---|---|
| GAME_WINNER / Moneyline | Game winner | `NCAAB_GAME_WINNER_FT` | two-team winner; no Draw |
| POINT_SPREAD | Point spread | `NCAAB_POINT_SPREAD_FT:{home_signed_line}` | exact `.5` only |
| TOTAL_POINTS | Total points | `NCAAB_TOTAL_POINTS_FT:{line}` | exact `.5` only |

Rejected / deferred: integer spread/total lines; halves / quarters; team totals; player props; winning margin; first score; conference / championship / March Madness outrights; bracket advancement; NCAAW; D2/D3; NBA/WNBA/G League.

Kalshi discovery prefixes are exact `KXNCAAMBGAME` / `KXNCAAMBSPREAD` / `KXNCAAMBTOTAL`. The short `KXNCAAMB` stem is not used.

Polymarket game book is Gamma sport `cbb` / series `10470`. Sport `ncaab` / series `39` is March Madness, not FIXTURE_MATCH.

Matchbook uses Basketball sport-id `4`. There is **no NCAA/NCAAB competition tag**. Current basketball listings (WNBA / NBA championship) are `rejected_non_ncaab_basketball`. Zero NCAAB fixtures is healthy.

---

## PAPER admission — all cells blocked

The 2026-09-22 census did not recover NCAAB-specific settlement equivalence for any venue pair.

| Pair | GAME_WINNER | x.5 SPREAD | x.5 TOTAL |
|---|---|---|---|
| Kalshi ↔ Polymarket | **BLOCKED** | **BLOCKED** | **BLOCKED** |
| Matchbook ↔ Kalshi | **BLOCKED** | **BLOCKED** | **BLOCKED** |
| Matchbook ↔ Polymarket | **BLOCKED** | **BLOCKED** | **BLOCKED** |
| three-venue | **BLOCKED** | **BLOCKED** | **BLOCKED** |

`ncaab_registered_canonical_key` always returns `None`. Soccer Matchbook↔Kalshi must not consume NCAAB markets. Automatic settlement is disabled (`ncaab_venue_pair_family_not_evidence_backed`). A participating venue that this agent cannot fetch (including Polymarket) is `ncaab_missing_participating_venue_evidence`, never success. Elapsed tipoff is not completion.

This does **not** copy NBA PR #506 settlement behaviour.

Audit marker: `exceptional_settlement_mismatch_possible`. Not live-execution-grade.

---

## Current live/off-season provider gaps (2026-09-22)

- Kalshi GAME/SPREAD/TOTAL series exist; **0 open events**. Nested settled GAME markets archived (404). Series GAME PDF is generic `ACHIEVEMENTS.pdf`.
- Polymarket `cbb` / `10470`: **0 open games**. Historical 2025-11-03 moneyline/spread/total exist.
- Matchbook: **no NCAAB tag**; basketball slate is WNBA + NBA championship outright.
- NCAA Division I 2026–27 opening night: **2026-11-02**.
