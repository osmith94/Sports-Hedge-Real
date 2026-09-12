# Historical odds repository

Source-neutral historical odds store for Sports Hedge analysis. Smarkets is
optional. The SQLite repository is the source of truth; Excel is a review
export only.

## Bounded universe (2025/26)

- Premier League
- Championship
- La Liga

Champions League 2025/26 is registered in the competition catalog so later
seasons/competitions can be added without a model change. It is not part of
the current coverage target.

## Canonical identity

Odds and historical match facts share one warehouse identity contract in
`sports_hedge.facts`. This is the same `hist:` uuid5 scheme used by PR #35:

```text
hist:{uuid5(NAMESPACE_URL, competition_id|season_id|home_team_id|away_team_id|kickoff_utc)}
```

- Competition IDs are `premier-league`, `championship`, `la-liga`, `champions-league`.
- Season IDs are `{competition_id}:2025-26`.
- Team IDs come from an explicit catalog. Unknown or ambiguous names fail closed
  and never mint a new canonical team.
- Kickoff for the ID seed is the UTC instant of an **aware** timestamp.
  Naive datetimes are rejected. Adapters that receive local times (for example
  football-data.co.uk) must attach a documented timezone and convert explicitly.
- Odds store an `odds_match_index` only as a coverage cache of canonical match
  IDs already seen. Match scores and events belong to the historical facts
  repository, not this odds store.

## Quality tiers

Tiers are assigned from what the source actually provided. The importer never
upgrades a quote.

| Tier | Meaning |
| --- | --- |
| A | timestamped exchange odds **and** liquidity |
| B | timestamped bookmaker odds |
| C | opening/closing odds only |
| D | match facts only / unavailable odds |

Opening/closing quotes stay tier C even if a kickoff time is known.
Liquidity and observation timestamps are stored only when the source
supplied them.

Incomplete settlement fingerprints (`semantics_complete=false`) cannot be
grouped with complete markets. `market_equivalence_key()` is `None` until
scope, period, extra-time and penalties flags are known.

## Adapters

All adapters implement `OddsSourceAdapter`. None is a hard dependency.

| Adapter | Role | Quality |
| --- | --- | --- |
| `synthetic` | In-repo fixtures for tests and the example report | A–D |
| `football_data` | Parses **local** football-data.co.uk style CSVs supplied by the operator | C (plus D facts rows) |
| `smarkets` | Optional. No network fetch. Authorised local snapshots only | skipped unless configured |

football-data.co.uk publishes public season CSVs. This repo does not download
them during tests or CI. Point `FootballDataCsvAdapter.from_path(...)` at a
file you already have.

## Smarkets limitations

Researched against the public Smarkets Trading API documentation
(`https://docs.smarkets.com/` / `https://api.smarkets.com`).

- The documented API covers **current** events, markets and odds.
- There is **no authorised bulk historical-odds archive** wired here.
- The adapter does **not** log in, scrape, page through undocumented
  endpoints, or bypass robots/CAPTCHA/paywalls/geo restrictions.
- `required = false`. Coverage and ingestion succeed with Smarkets absent.
- If an authorised snapshot is later supplied, sparse open/close quotes
  must be stored as quality C without inventing timestamps or liquidity.

## Ingestion

```bash
cd backend
python -m sports_hedge.odds.example_report
```

Imports are idempotent for exact replays. A corrected price or payload hash
writes a new observation (`source_observation_key` stays the same) instead of
overwriting the original row. Checkpoints live in `ingestion_checkpoints`.

## Coverage and Excel

Coverage is computed from the normalized repository:

- matches in repository vs expected league calendar size
- 1X2 opening/closing, totals, BTTS, corners, cards
- timestamped and liquidity history
- unresolved mapping count
- per-source availability (Smarkets listed even when empty)

Excel sheets:

1. Odds Observations
2. Market Coverage
3. Source Coverage
4. Mapping Exceptions
5. Quality Summary

See `docs/examples/historical-odds-coverage.md` for a synthetic example.
