# NBA PAPER families

**Parent roadmap:** #448  
**Evidence:** #453 / `docs/nba/01_PROVIDER_EVIDENCE_CENSUS.md`  
**Mode:** PAPER MODE · EXECUTION DISABLED · GET / read-only only  
**Data class:** captured Phase 1 provider fixtures (live public payloads from 2026-09-21) plus operator-scope registry. Not modelled probabilities. Not demo soccer data.

This layer adds a sport-specific NBA canonical/normalisation/register path that feeds the existing UNIVERSE → BACKGROUND → HOT → PAPER scanner. It does not duplicate the soccer or NFL scanner.

NBA is a permanently selectable UNIVERSE competition (`nba`). A selected NBA scope with zero listed events reports zero honestly. NBA is not in the default eight.

---

## Families onboarded (normalisers)

Exactly three full-game families:

| Family | Operator label | Canonical key | Line rule |
|---|---|---|---|
| GAME_WINNER / Moneyline | Game winner | `NBA_GAME_WINNER_FT` | two-team winner; no Draw runner |
| POINT_SPREAD | Point spread | `NBA_POINT_SPREAD_FT:{home_signed_line}` | exact `.5` only |
| TOTAL_POINTS | Total points | `NBA_TOTAL_POINTS_FT:{line}` | exact `.5` only |

Unsupported: integer spreads/totals; 1H/2H/quarters; overtime-only; team totals; player props; first basket / odd-even / exact margin; parlays; Summer League; WNBA/NCAAB; championship/series outrights.

## PAPER venue-pair matrix

Normal-completion PAPER equivalence is admitted only where #453 evidence supports the exact pair/family. Exceptional cancel/postpone/suspend/50-50/fair-price paths stay fail-closed via lifecycle audit. This is **not** live-execution-grade settlement equivalence.

| Family | Matchbook ↔ Kalshi | Matchbook ↔ Polymarket | Kalshi ↔ Polymarket |
|---|---|---|---|
| GAME_WINNER | **blocked** — no Matchbook NBA game book | **blocked** — no Matchbook NBA game book | **PAPER-admitted** — two-team winner, OT wording on both moneyline/PDF; exceptional lifecycle fail-closed |
| POINT_SPREAD x.5 | **blocked** | **blocked** | **blocked** — Polymarket spread payload silent on OT; no live Kalshi NBA spread ladder |
| TOTAL_POINTS x.5 | **blocked** | **blocked** | **blocked** — Polymarket total payload silent on OT; no live Kalshi NBA total ladder |

Normalisers still parse Kalshi SPREAD/TOTAL titles and Polymarket historical x.5 books for diagnostics/catalogue identity. Matchbook championship outright and WNBA analogue fixtures are rejected, not treated as NBA game proof.

Automatic PAPER settlement for the admitted Kalshi↔Polymarket GAME_WINNER cell requires **both** venues: exact-ID Kalshi market result **and** exact-ID Polymarket Gamma market (`umaResolutionStatus=resolved` plus `outcomePrices` 1/0). Kalshi-only evidence is not enough. Missing/unavailable Polymarket settlement evidence, 50-50/cancel/LFMP/unknown Polymarket lifecycle, and all POINT_SPREAD/TOTAL_POINTS trades stay fail-closed. Elapsed tipoff is never used.

## Provider identity retained for refresh

| Venue | Event | Market / runner |
|---|---|---|
| Kalshi | `event_ticker` + milestone `start_date` (never `occurrence_datetime`) | GAME: two YES tickers as runners; SPREAD/TOTAL: market ticker + YES/NO |
| Polymarket | Gamma event id | market id plus **exact CLOB token IDs**. Missing tokens, or invented `condition_id:0/1` placeholders, fail closed. |
| Matchbook | event id | market id + runner ids when a game book exists |

## Matchbook discovery load

NBA-only UNIVERSE scopes pass Matchbook `sport-ids=4` **and** competition tag `406202315670010`, so discovery does not download the whole basketball slate.

Mixed soccer/NFL/NBA scopes cannot apply that NBA tag without dropping football events. Those scopes keep basketball sport-id 4 on the same Matchbook `list_events` call (no extra concurrency slot) and reject WNBA/NCAAB/championship-outright payloads in the NBA normaliser. Follow-up: a second sequential NBA-tagged Matchbook query for mixed scopes, still on the existing Matchbook concurrency slot.

## Fees and concurrency

Fees resolve only through existing venue fee machinery (#503). No NBA/sport-specific fee table. Provider concurrency is unchanged (`DEFAULT_PROVIDER_CONCURRENCY`).
