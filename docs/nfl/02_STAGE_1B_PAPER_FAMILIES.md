# NFL Stage 1B — PAPER market families

**Issue:** #416  
**Parent roadmap:** #409  
**Evidence:** #410 / `docs/nfl/01_PROVIDER_EVIDENCE_CENSUS.md`  
**Base:** exact evidence head `3a7d1f02319c3fc5e858e7308eb28b76a1af60b3`  
**Mode:** PAPER MODE · EXECUTION DISABLED · GET / read-only only  
**Data class:** captured Phase 1 provider fixtures (live public payloads from 2026-09-20). Not modelled probabilities. Not demo soccer data.

This layer adds a sport-specific NFL canonical/normalisation/register path that feeds the existing UNIVERSE → BACKGROUND → HOT → PAPER scanner. It does not duplicate the soccer scanner.

---

## Owner-approved PAPER families

Exactly three full-game families:

| Family | Operator label | Canonical key | Line rule |
|---|---|---|---|
| GAME_WINNER / Moneyline | Game winner | `NFL_GAME_WINNER_FT` | two-team winner; no Draw runner |
| POINT_SPREAD | Point spread | `NFL_POINT_SPREAD_FT:{home_signed_line}` | exact `.5` only |
| TOTAL_POINTS | Total points | `NFL_TOTAL_POINTS_FT:{line}` | exact `.5` only |

Unsupported in this stage: integer spreads/totals; first half / second half / quarter; overtime-only series; team totals; player props; exact margin / first TD; parlays.

## Equivalence

Owner decision 2026-09-26: exceptional lifecycle differences are not an admission or automatic-settlement blocker. GAME_WINNER, exact `.5` spread, and exact `.5` total stay registered when fixture, family, line, and outcome space match.

New trades do not record `exceptional_settlement_mismatch_possible` or `nfl_paper_not_live_execution_equivalent`. Historical rows that already stored those strings remain readable. The historical paper label is not a catalogue execution veto. Venue orders stay behind `SPORTS_HEDGE_MODE=real` and `SPORTS_HEDGE_EXECUTION_ENABLED`.

Automatic PAPER settlement uses a graded result that determines the canonical outcome. A postponed or similar token in lifecycle history does not by itself block that result. A current cancelled/void status does not invent a winner (`provider_status_*`). A tied two-way score stays `canonical_outcome_not_determined`.

## Provider identity retained for refresh

| Venue | Event | Market / runner |
|---|---|---|
| Kalshi | `event_ticker` + milestone `start_date` (never `occurrence_datetime`) | GAME: two YES tickers as runners; SPREAD/TOTAL: market ticker + YES/NO |
| Polymarket | Gamma event id; `gameId` on the captured payload | market id plus **exact CLOB token IDs**. Missing tokens, or invented `condition_id:0/1` placeholders, fail closed and never enter the durable UNIVERSE/PAPER identity. |
| Matchbook | event id | market id, runner ids, participant ids on runners |

## Operator copy

- `KC -6.5` → `must win by 7+`
- `IND +6.5` → `may lose by up to 6, or win`
- `Over 47.5` → `48+ combined points`
- `Under 47.5` → `47 or fewer combined points`
