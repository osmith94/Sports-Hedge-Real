# Historical odds coverage example

Generated from the in-repo **synthetic** adapter for Premier League,
Championship and La Liga 2025/26. The SQLite repository is the source of
truth; this document is a review snapshot.

## Universe

- Premier League 2025/26
- Championship 2025/26
- La Liga 2025/26
- UEFA Champions League 2025/26 (schema only)

- Matches in repository: **4**
- Odds observations: **18**
- Unresolved mapping count: **0**

## Market coverage

| Competition | Season | Market | Matches | With market | Opening 1X2 | Closing 1X2 | Timestamped | Liquidity | Missing | Calendar gap |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| premier-league | 2025/26 | match_result | 2 | 2 | 0 | 2 | 1 | 1 | 0 | 378 |
| premier-league | 2025/26 | total_goals | 2 | 1 | 0 | 0 | 1 | 0 | 1 | 378 |
| premier-league | 2025/26 | both_teams_to_score | 2 | 1 | 0 | 0 | 1 | 0 | 1 | 378 |
| premier-league | 2025/26 | corners | 2 | 0 | 0 | 0 | 0 | 0 | 2 | 378 |
| premier-league | 2025/26 | cards | 2 | 0 | 0 | 0 | 0 | 0 | 2 | 378 |
| championship | 2025/26 | match_result | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 551 |
| championship | 2025/26 | total_goals | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 551 |
| championship | 2025/26 | both_teams_to_score | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 551 |
| championship | 2025/26 | corners | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 551 |
| championship | 2025/26 | cards | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 551 |
| la-liga | 2025/26 | match_result | 1 | 1 | 0 | 0 | 1 | 1 | 0 | 379 |
| la-liga | 2025/26 | total_goals | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 379 |
| la-liga | 2025/26 | both_teams_to_score | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 379 |
| la-liga | 2025/26 | corners | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 379 |
| la-liga | 2025/26 | cards | 1 | 0 | 0 | 0 | 0 | 0 | 1 | 379 |
| champions-league | 2025/26 | match_result | 0 | 0 | 0 | 0 | 0 | 0 | 0 |  |
| champions-league | 2025/26 | total_goals | 0 | 0 | 0 | 0 | 0 | 0 | 0 |  |
| champions-league | 2025/26 | both_teams_to_score | 0 | 0 | 0 | 0 | 0 | 0 | 0 |  |
| champions-league | 2025/26 | corners | 0 | 0 | 0 | 0 | 0 | 0 | 0 |  |
| champions-league | 2025/26 | cards | 0 | 0 | 0 | 0 | 0 | 0 | 0 |  |

## Source coverage

| Source | Required | Available | Observations | Matches | Timestamped | Liquidity | Notes |
|---|---|---|---:|---:|---:|---:|---|
| smarkets | False | False | 0 | 0 | 0 | 0 | Smarkets public/trading API surfaces current markets and odds. No authorised historical odds dump is wired here. Sparse open/close snapshots, when supplied locally, are stored as quality C without inventing timestamps or liquidity. |
| synthetic | False | True | 18 | 4 | 11 | 6 |  |

## Quality summary

| Tier | Observations | Matches | Meaning |
|---|---:|---:|---|
| A | 6 | 2 | timestamped exchange odds + liquidity |
| B | 5 | 1 | timestamped bookmaker odds |
| C | 6 | 2 | opening/closing odds only |
| D | 1 | 1 | non-odds match facts only / unavailable odds |

## Smarkets limitations

- Required: `False`
- Live historical archive: `False`
- Network fetch enabled: `False`
- Supported ingest: authorised local snapshot files only

Smarkets public/trading API surfaces current markets and odds. No authorised historical odds dump is wired here. Sparse open/close snapshots, when supplied locally, are stored as quality C without inventing timestamps or liquidity.

