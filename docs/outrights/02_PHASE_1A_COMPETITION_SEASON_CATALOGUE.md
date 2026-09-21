# Outrights Phase 1A — COMPETITION_SEASON identity + observation catalogue

**Issue:** #437  
**Parent:** #436  
**Base:** exact `owner-live` `588c8b41802690c226ce338e871ba17fcd367eab`  
**Mode:** PAPER · read-only venue boundary · no `owner-live` movement

This is the first production-safe outright foundation. It adds a sibling
canonical identity and durable exact native-ID catalogue support. It does
**not** admit cross-venue outright equivalence, change fixture identity, add a
second scanner, or increase provider concurrency.

## Canonical identity

```text
MarketScope
  FIXTURE_MATCH         # existing soccer / NFL game identity — unchanged
  COMPETITION_SEASON    # this slice
```

`CanonicalCompetitionSeasonRef` is a sibling of `CanonicalEvent`, not a
widening of it:

```text
sport
competition_code
season_id                 # exact token (PL `2026/27`, NFL `NFL-2026-SB-LXI`)
event_scope = COMPETITION_SEASON
market_family             # competition_winner | top_scorer | …
participant_type          # TEAM | PLAYER
participant_canonical_id
settlement_fingerprint_version
expected_settlement_horizon
```

Forbidden on COMPETITION_SEASON identity and catalogue rows:

- `home_team` / `away_team` / `kickoff_utc`
- fixture `canonical_match_id` / `EventMatcher`
- price / liquidity / odds fields

## Catalogue

The existing Approved Market Catalogue store is extended generically with
`market_scope`. FIXTURE_MATCH rows keep their unique `(canonical_event_id,
register_canonical_key)` contract and remain the only price-engine working-set
members.

COMPETITION_SEASON rows are **per-venue observation listings**:

- observation key `OBSERVATION:{family}:{venue}` (not an Approved Match Register key)
- exact Kalshi event/market tickers
- exact Matchbook event/market/runner IDs
- exact Polymarket event/market IDs **and both real CLOB token IDs**
- missing or placeholder CLOB tokens fail closed; IDs are never synthesized

Register status is derived as `UNKNOWN`. No path emits
`APPROVED_EQUIVALENT` or `PAPER_ASSUMED_EQUIVALENT` for these rows.

BACKGROUND/HOT exact-ID repricing can later consume the same persisted IDs.
Phase 1A excludes season rows from `derived_price_engine_working_set` so the
fixture price engine is unchanged.

## Initial observation targets

Captured public native IDs (2026-09-20 Phase 0A evidence), not owner-live quotes:

1. Premier League 2026-27 champion (TEAM)
2. NFL 2026 / Super Bowl champion (`NFL-2026-SB-LXI`, TEAM)
3. Premier League 2026-27 top scorer (PLAYER, venue-native ids, observation-only)

Top scorer Kalshi vs Polymarket remains fail-closed on joint-winner policy
(dead heat `$1/n` vs alphabetical sole winner).

## Explicitly out of this slice

- dual Min Net Arb / treasury caps (#428 / later)
- Approved Match Register onboarding / Phase 1C equivalence
- provider concurrency changes
- venue writes, merge, owner-live movement
- Polymarket exact-ID season fetches (composition-ready for #429; not NFL fixture GETs)

## UNIVERSE operator picker

COMPETITION_SEASON scopes participate in the existing #381 Universe scope
picker **already present on owner-live**. This PR extends that interface; it
does not vendor, cherry-pick, or re-implement the #381/#389/Vanilla stack.

Selecting a season row makes it eligible for the **existing** UNIVERSE
worker via extra Kalshi series tickers on that generation. Deselecting stops
new discovery/admission for that scope and does not delete catalogue history
or abandon OPEN/PARTIAL/ACTIVE PAPER positions.

Scope Apply remains provider-I/O-free unless the operator chooses Run UNIVERSE
now. There is no second outright scanner. Cross-venue outright equivalence and
PAPER admission stay closed. Top scorer remains observation-only / not
executable while joint-winner policy is fail-closed.

### Integration dependency

Compose this slice onto exact owner-live `588c8b41802690c226ce338e871ba17fcd367eab`
(GitHub branch `owner-live`), which already carries the #381 picker
(`OperatorUniverseScope`, `/paper/universe-scope`,
`FootballCompetitionsModal`). Final Vanilla + NFL + Outrights integration
must use that same picker contract:

- FIXTURE_MATCH rows stay `selected_competition_codes`
- COMPETITION_SEASON rows stay `selected_season_scope_codes`
- catalog options carry `market_scope`
- NFL game fixtures (#429) must enter as FIXTURE_MATCH picker rows, not by
  copying `KXNFLGAME` logic into the outright layer

Do not open or review this PR against stale `main`; that comparison embeds
unrelated owner-live history and is not the Outrights lane.
