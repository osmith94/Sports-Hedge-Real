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

## Equivalence and caveat

Normal completed-game payoff is owner-approved for PAPER comparison when canonical fixture + family + exact line/side match.

Audit marker: `exceptional_settlement_mismatch_possible`.

This is **not** live-execution-grade settlement equivalence. Cancellation, suspension, and final-tie handling can differ across venues. Automatic settlement fail-closes on those exceptional cases.

## Provider identity retained for refresh

| Venue | Event | Market / runner |
|---|---|---|
| Kalshi | `event_ticker` + milestone `start_date` (never `occurrence_datetime`) | GAME: two YES tickers as runners; SPREAD/TOTAL: market ticker + YES/NO |
| Polymarket | Gamma event id; `gameId` on the captured payload | market id, `conditionId`, CLOB token IDs when present |
| Matchbook | event id | market id, runner ids, participant ids on runners |

## Operator copy

- `KC -6.5` → `must win by 7+`
- `IND +6.5` → `may lose by up to 6, or win`
- `Over 47.5` → `48+ combined points`
- `Under 47.5` → `47 or fewer combined points`
