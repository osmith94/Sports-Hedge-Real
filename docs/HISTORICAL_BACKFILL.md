# Football-Data.co.uk England backfill (five seasons)

Operator-invoked import of the **public** Football-Data.co.uk season CSVs
into the merged historical facts and odds repositories.

Season mapping is code-driven (`2122`…`2526`); the backfill does not special-case
ten one-off files. 2025/26 is the verification slice (`--seasons 2526`); the
same path then imports 2021/22–2024/25.

## Source URLs (explicit, unauthenticated)

`https://www.football-data.co.uk/mmz4281/{season_code}/{div}.csv`

| Season | Code | Premier League | Championship |
|---|---|---|---|
| 2021/22 | 2122 | https://www.football-data.co.uk/mmz4281/2122/E0.csv | https://www.football-data.co.uk/mmz4281/2122/E1.csv |
| 2022/23 | 2223 | https://www.football-data.co.uk/mmz4281/2223/E0.csv | https://www.football-data.co.uk/mmz4281/2223/E1.csv |
| 2023/24 | 2324 | https://www.football-data.co.uk/mmz4281/2324/E0.csv | https://www.football-data.co.uk/mmz4281/2324/E1.csv |
| 2024/25 | 2425 | https://www.football-data.co.uk/mmz4281/2425/E0.csv | https://www.football-data.co.uk/mmz4281/2425/E1.csv |
| 2025/26 | 2526 | https://www.football-data.co.uk/mmz4281/2526/E0.csv | https://www.football-data.co.uk/mmz4281/2526/E1.csv |

The command issues a single GET per file. It does not scrape HTML, log in,
or use undocumented endpoints. Each imported row retains that file's URL.

## What is imported (real)

- Match identity through `sports_hedge.facts` + `HistoricalCatalog` (fail closed)
- FT/HT scores
- Corners, yellow cards, red cards when present
- Opening and closing quotes as **distinct quality-C snapshots** (`observed_at` empty)
- Header-driven 1X2, over/under 2.5, and Asian handicap columns actually present
  for each file (bookmaker-specific quotes kept separate)
- Max/Avg (and Betfair Exchange `BFE` where labelled) as **aggregate/research-only**
- Derived implied probability and logit; same-line opening→closing deltas are
  review fields only. Changed Asian-handicap (or other) lines are stored as
  structural line shifts, not as price-move analogues.
- Source URL, retrieval time, row identity, payload hash, Europe/London kickoff → UTC

A complete five-season PL + Championship league calendar is **4660** matches
(5×380 + 5×552). Report the actual imported count; do not force that number.

## What is unavailable from this source (not invented)

- Goal minutes / event timelines
- Lineups and substitutions
- Penalty counts
- Liquidity, commission
- Intraday / timestamped odds paths (`observed_at` stays empty)
- Smarkets (optional and unused for this slice)

## How to run

CI must not fetch. Tests use pinned local fixtures.

```bash
cd backend
# Network, operator-invoked (all five seasons):
python -m sports_hedge.backfill.football_data \
  --facts-db ../data/historical_football.sqlite \
  --odds-db ../data/historical_odds.sqlite \
  --output-dir ../docs/examples \
  --fetch

# Verify 2025/26 first, then remaining seasons:
python -m sports_hedge.backfill.football_data ... --fetch --seasons 2526
python -m sports_hedge.backfill.football_data ... --fetch --seasons 2122,2223,2324,2425

# Offline: {season_code}/E0.csv or {season_code}_{div}.csv
python -m sports_hedge.backfill.football_data \
  --facts-db ../data/historical_football.sqlite \
  --odds-db ../data/historical_odds.sqlite \
  --output-dir ../docs/examples \
  --local-dir /path/to/csv
```

The job fails if a file is empty/truncated or mapping exceptions exceed 5 per file.
Exact reruns are idempotent; changed source content remains append-only.

Review artefacts: `docs/examples/football-data-england-coverage.md` plus facts/odds
xlsx. Those are exports; SQLite is the source of truth. The committed odds workbook
is coverage/quality sheets only (full per-observation rows stay in SQLite because a
five-season dump is too large for the git review copy).
