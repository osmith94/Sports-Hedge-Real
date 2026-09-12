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

`sports_hedge.facts` on `main` (merged PR #35,
`d0e2a323d18f79ee52ac88663599e1e06c65c02c`) owns match/team/competition
identity. This odds package does not vendor or redefine that contract.

Canonical match IDs are:

```text
match:{sha256(football|competition|season|home|away|kickoff_bucket)[:24]}
```

Competition codes are `premier_league`, `championship`, `la_liga`,
`champions_league`. Odds mapping calls `facts.identity.build_match_ref`
after fail-closed team resolution through `historical.catalog.HistoricalCatalog`
(explicit `facts` aliases of catalogued teams are allowed; unknown names are
rejected).

Cross-module regression: Arsenal vs Chelsea 2025-08-16 17:30 UTC is
`match:f295bd6ca68b6926073e179d` in both the facts repository and odds
observations.

Odds-only rules:

- Naive quote/kickoff/retrieval timestamps are rejected. football-data.co.uk
  local kickoffs are converted to UTC inside the adapter with documented
  timezone provenance.
- `odds_match_index` is a coverage cache of canonical match IDs already
  seen. It is not a second match-facts store. `sports_hedge.historical` remains
  the canonical match denominator.
- Opening/closing quotes stay quality C and have no invented `observed_at`.
  Timestamped paths needed for Core Tenet 17 movement context are stored only
  when the source supplied `observed_at`. Implied probability/logit are derived
  from stored decimal odds; this package does not run the analogue engine.

## Coverage honesty

| Kind | What it is |
| --- | --- |
| Fixture / demo | In-repo `synthetic` adapter, `docs/examples/historical-odds-coverage.md`, and `docs/examples/historical-odds-export.xlsx` |
| Operator-supplied local CSV | football-data.co.uk **style** files the operator already has locally (quality C). Not downloaded in CI |
| Production historical odds | **Not collected in this PR.** Smarkets is optional and has no authorised bulk archive wired here |

The example Excel/markdown files are synthetic review snapshots, not live
coverage of 2025/26.

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
