# Football-Data.co.uk England backfill (five seasons)

Repository-derived review of **real** public Football-Data files for
Premier League (`E0`) and Championship (`E1`), seasons 2021/22–2025/26.
The SQLite databases are the source of truth; Excel is export-only.

- Retrieved at (UTC): `2026-09-12T12:15:53.704813+00:00`
- Kickoff timezone assumption: Europe/London, converted to UTC in the adapter
- Odds quality: opening/closing snapshots only (**tier C**, `observed_at` empty)
- Max/Avg (and BFE where labelled research-only) are **aggregate/research-only**, not executable venue quotes
- Unavailable from this source: goal minutes, lineups, substitutions, penalties, liquidity, commission, intraday quote timestamps
- Smarkets: not used
- Sanity check: complete 5×PL + 5×Championship league calendars would be **4660** matches; imported facts matches: **4660**

## Source files

| Season | Code | Div | Competition | Public URL | CSV rows | Expected | Facts matches | Odds observations created | Mapping exceptions |
|---|---|---|---|---|---:|---:|---:|---:|---:|
| 2021/22 | 2122 | E0 | Premier League | https://www.football-data.co.uk/mmz4281/2122/E0.csv | 380 | 380 | 380 | 30765 | 0 |
| 2021/22 | 2122 | E1 | Championship | https://www.football-data.co.uk/mmz4281/2122/E1.csv | 552 | 552 | 552 | 44712 | 0 |
| 2022/23 | 2223 | E0 | Premier League | https://www.football-data.co.uk/mmz4281/2223/E0.csv | 380 | 380 | 380 | 30776 | 0 |
| 2022/23 | 2223 | E1 | Championship | https://www.football-data.co.uk/mmz4281/2223/E1.csv | 552 | 552 | 552 | 44679 | 0 |
| 2023/24 | 2324 | E0 | Premier League | https://www.football-data.co.uk/mmz4281/2324/E0.csv | 380 | 380 | 380 | 29614 | 0 |
| 2023/24 | 2324 | E1 | Championship | https://www.football-data.co.uk/mmz4281/2324/E1.csv | 552 | 552 | 552 | 43224 | 0 |
| 2024/25 | 2425 | E0 | Premier League | https://www.football-data.co.uk/mmz4281/2425/E0.csv | 380 | 380 | 380 | 32407 | 0 |
| 2024/25 | 2425 | E1 | Championship | https://www.football-data.co.uk/mmz4281/2425/E1.csv | 552 | 552 | 552 | 47377 | 0 |
| 2025/26 | 2526 | E0 | Premier League | https://www.football-data.co.uk/mmz4281/2526/E0.csv | 380 | 380 | 380 | 36690 | 0 |
| 2025/26 | 2526 | E1 | Championship | https://www.football-data.co.uk/mmz4281/2526/E1.csv | 552 | 552 | 552 | 52820 | 0 |

## Facts field coverage

| Competition | Season | Field | Matches | Present | Ratio |
|---|---|---|---:|---:|---:|
| Championship | 2021/22 | ft_score | 552 | 552 | 100.00% |
| Championship | 2021/22 | ht_score | 552 | 552 | 100.00% |
| Championship | 2021/22 | goal_timestamps | 552 | 44 | 7.97% |
| Championship | 2021/22 | corners | 552 | 552 | 100.00% |
| Championship | 2021/22 | yellow_cards | 552 | 552 | 100.00% |
| Championship | 2021/22 | red_cards | 552 | 552 | 100.00% |
| Championship | 2021/22 | penalties | 552 | 0 | 0.00% |
| Championship | 2021/22 | substitutions | 552 | 0 | 0.00% |
| Championship | 2021/22 | lineups | 552 | 0 | 0.00% |
| Championship | 2022/23 | ft_score | 552 | 552 | 100.00% |
| Championship | 2022/23 | ht_score | 552 | 552 | 100.00% |
| Championship | 2022/23 | goal_timestamps | 552 | 48 | 8.70% |
| Championship | 2022/23 | corners | 552 | 552 | 100.00% |
| Championship | 2022/23 | yellow_cards | 552 | 552 | 100.00% |
| Championship | 2022/23 | red_cards | 552 | 552 | 100.00% |
| Championship | 2022/23 | penalties | 552 | 0 | 0.00% |
| Championship | 2022/23 | substitutions | 552 | 0 | 0.00% |
| Championship | 2022/23 | lineups | 552 | 0 | 0.00% |
| Championship | 2023/24 | ft_score | 552 | 552 | 100.00% |
| Championship | 2023/24 | ht_score | 552 | 552 | 100.00% |
| Championship | 2023/24 | goal_timestamps | 552 | 35 | 6.34% |
| Championship | 2023/24 | corners | 552 | 552 | 100.00% |
| Championship | 2023/24 | yellow_cards | 552 | 552 | 100.00% |
| Championship | 2023/24 | red_cards | 552 | 552 | 100.00% |
| Championship | 2023/24 | penalties | 552 | 0 | 0.00% |
| Championship | 2023/24 | substitutions | 552 | 0 | 0.00% |
| Championship | 2023/24 | lineups | 552 | 0 | 0.00% |
| Championship | 2024/25 | ft_score | 552 | 552 | 100.00% |
| Championship | 2024/25 | ht_score | 552 | 552 | 100.00% |
| Championship | 2024/25 | goal_timestamps | 552 | 44 | 7.97% |
| Championship | 2024/25 | corners | 552 | 552 | 100.00% |
| Championship | 2024/25 | yellow_cards | 552 | 552 | 100.00% |
| Championship | 2024/25 | red_cards | 552 | 552 | 100.00% |
| Championship | 2024/25 | penalties | 552 | 0 | 0.00% |
| Championship | 2024/25 | substitutions | 552 | 0 | 0.00% |
| Championship | 2024/25 | lineups | 552 | 0 | 0.00% |
| Championship | 2025/26 | ft_score | 552 | 552 | 100.00% |
| Championship | 2025/26 | ht_score | 552 | 552 | 100.00% |
| Championship | 2025/26 | goal_timestamps | 552 | 34 | 6.16% |
| Championship | 2025/26 | corners | 552 | 552 | 100.00% |
| Championship | 2025/26 | yellow_cards | 552 | 552 | 100.00% |
| Championship | 2025/26 | red_cards | 552 | 552 | 100.00% |
| Championship | 2025/26 | penalties | 552 | 0 | 0.00% |
| Championship | 2025/26 | substitutions | 552 | 0 | 0.00% |
| Championship | 2025/26 | lineups | 552 | 0 | 0.00% |
| Premier League | 2021/22 | ft_score | 380 | 380 | 100.00% |
| Premier League | 2021/22 | ht_score | 380 | 380 | 100.00% |
| Premier League | 2021/22 | goal_timestamps | 380 | 22 | 5.79% |
| Premier League | 2021/22 | corners | 380 | 380 | 100.00% |
| Premier League | 2021/22 | yellow_cards | 380 | 380 | 100.00% |
| Premier League | 2021/22 | red_cards | 380 | 380 | 100.00% |
| Premier League | 2021/22 | penalties | 380 | 0 | 0.00% |
| Premier League | 2021/22 | substitutions | 380 | 0 | 0.00% |
| Premier League | 2021/22 | lineups | 380 | 0 | 0.00% |
| Premier League | 2022/23 | ft_score | 380 | 380 | 100.00% |
| Premier League | 2022/23 | ht_score | 380 | 380 | 100.00% |
| Premier League | 2022/23 | goal_timestamps | 380 | 23 | 6.05% |
| Premier League | 2022/23 | corners | 380 | 380 | 100.00% |
| Premier League | 2022/23 | yellow_cards | 380 | 380 | 100.00% |
| Premier League | 2022/23 | red_cards | 380 | 380 | 100.00% |
| Premier League | 2022/23 | penalties | 380 | 0 | 0.00% |
| Premier League | 2022/23 | substitutions | 380 | 0 | 0.00% |
| Premier League | 2022/23 | lineups | 380 | 0 | 0.00% |
| Premier League | 2023/24 | ft_score | 380 | 380 | 100.00% |
| Premier League | 2023/24 | ht_score | 380 | 380 | 100.00% |
| Premier League | 2023/24 | goal_timestamps | 380 | 11 | 2.89% |
| Premier League | 2023/24 | corners | 380 | 380 | 100.00% |
| Premier League | 2023/24 | yellow_cards | 380 | 380 | 100.00% |
| Premier League | 2023/24 | red_cards | 380 | 380 | 100.00% |
| Premier League | 2023/24 | penalties | 380 | 0 | 0.00% |
| Premier League | 2023/24 | substitutions | 380 | 0 | 0.00% |
| Premier League | 2023/24 | lineups | 380 | 0 | 0.00% |
| Premier League | 2024/25 | ft_score | 380 | 380 | 100.00% |
| Premier League | 2024/25 | ht_score | 380 | 380 | 100.00% |
| Premier League | 2024/25 | goal_timestamps | 380 | 16 | 4.21% |
| Premier League | 2024/25 | corners | 380 | 380 | 100.00% |
| Premier League | 2024/25 | yellow_cards | 380 | 380 | 100.00% |
| Premier League | 2024/25 | red_cards | 380 | 380 | 100.00% |
| Premier League | 2024/25 | penalties | 380 | 0 | 0.00% |
| Premier League | 2024/25 | substitutions | 380 | 0 | 0.00% |
| Premier League | 2024/25 | lineups | 380 | 0 | 0.00% |
| Premier League | 2025/26 | ft_score | 380 | 380 | 100.00% |
| Premier League | 2025/26 | ht_score | 380 | 380 | 100.00% |
| Premier League | 2025/26 | goal_timestamps | 380 | 27 | 7.11% |
| Premier League | 2025/26 | corners | 380 | 380 | 100.00% |
| Premier League | 2025/26 | yellow_cards | 380 | 380 | 100.00% |
| Premier League | 2025/26 | red_cards | 380 | 380 | 100.00% |
| Premier League | 2025/26 | penalties | 380 | 0 | 0.00% |
| Premier League | 2025/26 | substitutions | 380 | 0 | 0.00% |
| Premier League | 2025/26 | lineups | 380 | 0 | 0.00% |

## Odds by league / season / market / bookmaker / quote type

| Competition | Season | Market | Bookmaker | Quote type | Observations | Research-only |
|---|---|---|---|---|---:|---|
| championship | 2021/22 | asian_handicap | avg | closing | 1104 | yes |
| championship | 2021/22 | asian_handicap | avg | opening | 1104 | yes |
| championship | 2021/22 | asian_handicap | b365 | closing | 1104 | no |
| championship | 2021/22 | asian_handicap | b365 | opening | 1104 | no |
| championship | 2021/22 | asian_handicap | max | closing | 1104 | yes |
| championship | 2021/22 | asian_handicap | max | opening | 1104 | yes |
| championship | 2021/22 | asian_handicap | p | closing | 1104 | no |
| championship | 2021/22 | asian_handicap | p | opening | 1104 | no |
| championship | 2021/22 | match_result | avg | closing | 1656 | yes |
| championship | 2021/22 | match_result | avg | opening | 1656 | yes |
| championship | 2021/22 | match_result | b365 | closing | 1656 | no |
| championship | 2021/22 | match_result | b365 | opening | 1656 | no |
| championship | 2021/22 | match_result | bw | closing | 1656 | no |
| championship | 2021/22 | match_result | bw | opening | 1656 | no |
| championship | 2021/22 | match_result | iw | closing | 1656 | no |
| championship | 2021/22 | match_result | iw | opening | 1656 | no |
| championship | 2021/22 | match_result | max | closing | 1656 | yes |
| championship | 2021/22 | match_result | max | opening | 1656 | yes |
| championship | 2021/22 | match_result | ps | closing | 1656 | no |
| championship | 2021/22 | match_result | ps | opening | 1656 | no |
| championship | 2021/22 | match_result | vc | closing | 1656 | no |
| championship | 2021/22 | match_result | vc | opening | 1656 | no |
| championship | 2021/22 | match_result | wh | closing | 1656 | no |
| championship | 2021/22 | match_result | wh | opening | 1656 | no |
| championship | 2021/22 | total_goals | avg | closing | 1104 | yes |
| championship | 2021/22 | total_goals | avg | opening | 1104 | yes |
| championship | 2021/22 | total_goals | b365 | closing | 1104 | no |
| championship | 2021/22 | total_goals | b365 | opening | 1104 | no |
| championship | 2021/22 | total_goals | max | closing | 1104 | yes |
| championship | 2021/22 | total_goals | max | opening | 1104 | yes |
| championship | 2021/22 | total_goals | p | closing | 1104 | no |
| championship | 2021/22 | total_goals | p | opening | 1104 | no |
| championship | 2022/23 | asian_handicap | avg | closing | 1104 | yes |
| championship | 2022/23 | asian_handicap | avg | opening | 1104 | yes |
| championship | 2022/23 | asian_handicap | b365 | closing | 1104 | no |
| championship | 2022/23 | asian_handicap | b365 | opening | 1104 | no |
| championship | 2022/23 | asian_handicap | max | closing | 1104 | yes |
| championship | 2022/23 | asian_handicap | max | opening | 1104 | yes |
| championship | 2022/23 | asian_handicap | p | closing | 1104 | no |
| championship | 2022/23 | asian_handicap | p | opening | 1104 | no |
| championship | 2022/23 | match_result | avg | closing | 1656 | yes |
| championship | 2022/23 | match_result | avg | opening | 1656 | yes |
| championship | 2022/23 | match_result | b365 | closing | 1656 | no |
| championship | 2022/23 | match_result | b365 | opening | 1656 | no |
| championship | 2022/23 | match_result | bw | closing | 1656 | no |
| championship | 2022/23 | match_result | bw | opening | 1656 | no |
| championship | 2022/23 | match_result | iw | closing | 1656 | no |
| championship | 2022/23 | match_result | iw | opening | 1623 | no |
| championship | 2022/23 | match_result | max | closing | 1656 | yes |
| championship | 2022/23 | match_result | max | opening | 1656 | yes |
| championship | 2022/23 | match_result | ps | closing | 1656 | no |
| championship | 2022/23 | match_result | ps | opening | 1656 | no |
| championship | 2022/23 | match_result | vc | closing | 1656 | no |
| championship | 2022/23 | match_result | vc | opening | 1656 | no |
| championship | 2022/23 | match_result | wh | closing | 1656 | no |
| championship | 2022/23 | match_result | wh | opening | 1656 | no |
| championship | 2022/23 | total_goals | avg | closing | 1104 | yes |
| championship | 2022/23 | total_goals | avg | opening | 1104 | yes |
| championship | 2022/23 | total_goals | b365 | closing | 1104 | no |
| championship | 2022/23 | total_goals | b365 | opening | 1104 | no |
| championship | 2022/23 | total_goals | max | closing | 1104 | yes |
| championship | 2022/23 | total_goals | max | opening | 1104 | yes |
| championship | 2022/23 | total_goals | p | closing | 1104 | no |
| championship | 2022/23 | total_goals | p | opening | 1104 | no |
| championship | 2023/24 | asian_handicap | avg | closing | 1104 | yes |
| championship | 2023/24 | asian_handicap | avg | opening | 1104 | yes |
| championship | 2023/24 | asian_handicap | b365 | closing | 1104 | no |
| championship | 2023/24 | asian_handicap | b365 | opening | 1104 | no |
| championship | 2023/24 | asian_handicap | max | closing | 1104 | yes |
| championship | 2023/24 | asian_handicap | max | opening | 1104 | yes |
| championship | 2023/24 | asian_handicap | p | closing | 1104 | no |
| championship | 2023/24 | asian_handicap | p | opening | 1104 | no |
| championship | 2023/24 | match_result | avg | closing | 1656 | yes |
| championship | 2023/24 | match_result | avg | opening | 1656 | yes |
| championship | 2023/24 | match_result | b365 | closing | 1656 | no |
| championship | 2023/24 | match_result | b365 | opening | 1656 | no |
| championship | 2023/24 | match_result | bw | closing | 1614 | no |
| championship | 2023/24 | match_result | bw | opening | 1650 | no |
| championship | 2023/24 | match_result | iw | closing | 936 | no |
| championship | 2023/24 | match_result | iw | opening | 936 | no |
| championship | 2023/24 | match_result | max | closing | 1656 | yes |
| championship | 2023/24 | match_result | max | opening | 1656 | yes |
| championship | 2023/24 | match_result | ps | closing | 1656 | no |
| championship | 2023/24 | match_result | ps | opening | 1656 | no |
| championship | 2023/24 | match_result | vc | closing | 1656 | no |
| championship | 2023/24 | match_result | vc | opening | 1656 | no |
| championship | 2023/24 | match_result | wh | closing | 1656 | no |
| championship | 2023/24 | match_result | wh | opening | 1656 | no |
| championship | 2023/24 | total_goals | avg | closing | 1104 | yes |
| championship | 2023/24 | total_goals | avg | opening | 1104 | yes |
| championship | 2023/24 | total_goals | b365 | closing | 1104 | no |
| championship | 2023/24 | total_goals | b365 | opening | 1104 | no |
| championship | 2023/24 | total_goals | max | closing | 1104 | yes |
| championship | 2023/24 | total_goals | max | opening | 1104 | yes |
| championship | 2023/24 | total_goals | p | closing | 1104 | no |
| championship | 2023/24 | total_goals | p | opening | 1104 | no |
| championship | 2024/25 | asian_handicap | avg | closing | 1104 | yes |
| championship | 2024/25 | asian_handicap | avg | opening | 1104 | yes |
| championship | 2024/25 | asian_handicap | b365 | closing | 1104 | no |
| championship | 2024/25 | asian_handicap | b365 | opening | 1104 | no |
| championship | 2024/25 | asian_handicap | bfe | closing | 1102 | yes |
| championship | 2024/25 | asian_handicap | bfe | opening | 1050 | yes |
| championship | 2024/25 | asian_handicap | max | closing | 1104 | yes |
| championship | 2024/25 | asian_handicap | max | opening | 1104 | yes |
| championship | 2024/25 | asian_handicap | p | closing | 1104 | no |
| championship | 2024/25 | asian_handicap | p | opening | 1104 | no |
| championship | 2024/25 | match_result | avg | closing | 1656 | yes |
| championship | 2024/25 | match_result | avg | opening | 1656 | yes |
| championship | 2024/25 | match_result | b365 | closing | 1656 | no |
| championship | 2024/25 | match_result | b365 | opening | 1656 | no |
| championship | 2024/25 | match_result | bf | closing | 1656 | no |
| championship | 2024/25 | match_result | bf | opening | 1656 | no |
| championship | 2024/25 | match_result | bfe | closing | 1656 | yes |
| championship | 2024/25 | match_result | bfe | opening | 1656 | yes |
| championship | 2024/25 | match_result | bw | closing | 1104 | no |
| championship | 2024/25 | match_result | bw | opening | 1104 | no |
| championship | 2024/25 | match_result | max | closing | 1656 | yes |
| championship | 2024/25 | match_result | max | opening | 1656 | yes |
| championship | 2024/25 | match_result | ps | closing | 1656 | no |
| championship | 2024/25 | match_result | ps | opening | 1656 | no |
| championship | 2024/25 | match_result | wh | closing | 1368 | no |
| championship | 2024/25 | match_result | wh | opening | 1365 | no |
| championship | 2024/25 | total_goals | avg | closing | 1104 | yes |
| championship | 2024/25 | total_goals | avg | opening | 1104 | yes |
| championship | 2024/25 | total_goals | b365 | closing | 1104 | no |
| championship | 2024/25 | total_goals | b365 | opening | 1104 | no |
| championship | 2024/25 | total_goals | bfe | closing | 1104 | yes |
| championship | 2024/25 | total_goals | bfe | opening | 1094 | yes |
| championship | 2024/25 | total_goals | max | closing | 1104 | yes |
| championship | 2024/25 | total_goals | max | opening | 1104 | yes |
| championship | 2024/25 | total_goals | p | closing | 1104 | no |
| championship | 2024/25 | total_goals | p | opening | 1102 | no |
| championship | 2025/26 | asian_handicap | avg | closing | 1104 | yes |
| championship | 2025/26 | asian_handicap | avg | opening | 1104 | yes |
| championship | 2025/26 | asian_handicap | b365 | closing | 1104 | no |
| championship | 2025/26 | asian_handicap | b365 | opening | 1102 | no |
| championship | 2025/26 | asian_handicap | bfe | closing | 1032 | yes |
| championship | 2025/26 | asian_handicap | bfe | opening | 974 | yes |
| championship | 2025/26 | asian_handicap | max | closing | 1104 | yes |
| championship | 2025/26 | asian_handicap | max | opening | 1104 | yes |
| championship | 2025/26 | asian_handicap | p | closing | 542 | no |
| championship | 2025/26 | asian_handicap | p | opening | 520 | no |
| championship | 2025/26 | match_result | avg | closing | 1656 | yes |
| championship | 2025/26 | match_result | avg | opening | 1656 | yes |
| championship | 2025/26 | match_result | b365 | closing | 1656 | no |
| championship | 2025/26 | match_result | b365 | opening | 1656 | no |
| championship | 2025/26 | match_result | bfd | closing | 1638 | no |
| championship | 2025/26 | match_result | bfd | opening | 1656 | no |
| championship | 2025/26 | match_result | bfe | closing | 1548 | yes |
| championship | 2025/26 | match_result | bfe | opening | 1548 | yes |
| championship | 2025/26 | match_result | bmgm | closing | 1656 | no |
| championship | 2025/26 | match_result | bmgm | opening | 1656 | no |
| championship | 2025/26 | match_result | bv | closing | 1638 | no |
| championship | 2025/26 | match_result | bv | opening | 1656 | no |
| championship | 2025/26 | match_result | bw | closing | 1656 | no |
| championship | 2025/26 | match_result | bw | opening | 1629 | no |
| championship | 2025/26 | match_result | cl | closing | 1194 | no |
| championship | 2025/26 | match_result | cl | opening | 1284 | no |
| championship | 2025/26 | match_result | lb | closing | 1260 | no |
| championship | 2025/26 | match_result | lb | opening | 1284 | no |
| championship | 2025/26 | match_result | max | closing | 1656 | yes |
| championship | 2025/26 | match_result | max | opening | 1656 | yes |
| championship | 2025/26 | match_result | ps | closing | 813 | no |
| championship | 2025/26 | match_result | ps | opening | 780 | no |
| championship | 2025/26 | total_goals | avg | closing | 1104 | yes |
| championship | 2025/26 | total_goals | avg | opening | 1104 | yes |
| championship | 2025/26 | total_goals | b365 | closing | 1104 | no |
| championship | 2025/26 | total_goals | b365 | opening | 1104 | no |
| championship | 2025/26 | total_goals | bfe | closing | 1032 | yes |
| championship | 2025/26 | total_goals | bfe | opening | 1028 | yes |
| championship | 2025/26 | total_goals | max | closing | 1104 | yes |
| championship | 2025/26 | total_goals | max | opening | 1104 | yes |
| championship | 2025/26 | total_goals | p | closing | 542 | no |
| championship | 2025/26 | total_goals | p | opening | 520 | no |
| premier_league | 2021/22 | asian_handicap | avg | closing | 760 | yes |
| premier_league | 2021/22 | asian_handicap | avg | opening | 760 | yes |
| premier_league | 2021/22 | asian_handicap | b365 | closing | 758 | no |
| premier_league | 2021/22 | asian_handicap | b365 | opening | 760 | no |
| premier_league | 2021/22 | asian_handicap | max | closing | 760 | yes |
| premier_league | 2021/22 | asian_handicap | max | opening | 760 | yes |
| premier_league | 2021/22 | asian_handicap | p | closing | 760 | no |
| premier_league | 2021/22 | asian_handicap | p | opening | 760 | no |
| premier_league | 2021/22 | match_result | avg | closing | 1140 | yes |
| premier_league | 2021/22 | match_result | avg | opening | 1140 | yes |
| premier_league | 2021/22 | match_result | b365 | closing | 1140 | no |
| premier_league | 2021/22 | match_result | b365 | opening | 1140 | no |
| premier_league | 2021/22 | match_result | bw | closing | 1140 | no |
| premier_league | 2021/22 | match_result | bw | opening | 1140 | no |
| premier_league | 2021/22 | match_result | iw | closing | 1131 | no |
| premier_league | 2021/22 | match_result | iw | opening | 1140 | no |
| premier_league | 2021/22 | match_result | max | closing | 1140 | yes |
| premier_league | 2021/22 | match_result | max | opening | 1140 | yes |
| premier_league | 2021/22 | match_result | ps | closing | 1140 | no |
| premier_league | 2021/22 | match_result | ps | opening | 1140 | no |
| premier_league | 2021/22 | match_result | vc | closing | 1140 | no |
| premier_league | 2021/22 | match_result | vc | opening | 1140 | no |
| premier_league | 2021/22 | match_result | wh | closing | 1140 | no |
| premier_league | 2021/22 | match_result | wh | opening | 1140 | no |
| premier_league | 2021/22 | total_goals | avg | closing | 760 | yes |
| premier_league | 2021/22 | total_goals | avg | opening | 760 | yes |
| premier_league | 2021/22 | total_goals | b365 | closing | 758 | no |
| premier_league | 2021/22 | total_goals | b365 | opening | 758 | no |
| premier_league | 2021/22 | total_goals | max | closing | 760 | yes |
| premier_league | 2021/22 | total_goals | max | opening | 760 | yes |
| premier_league | 2021/22 | total_goals | p | closing | 760 | no |
| premier_league | 2021/22 | total_goals | p | opening | 760 | no |
| premier_league | 2022/23 | asian_handicap | avg | closing | 760 | yes |
| premier_league | 2022/23 | asian_handicap | avg | opening | 760 | yes |
| premier_league | 2022/23 | asian_handicap | b365 | closing | 760 | no |
| premier_league | 2022/23 | asian_handicap | b365 | opening | 760 | no |
| premier_league | 2022/23 | asian_handicap | max | closing | 760 | yes |
| premier_league | 2022/23 | asian_handicap | max | opening | 760 | yes |
| premier_league | 2022/23 | asian_handicap | p | closing | 760 | no |
| premier_league | 2022/23 | asian_handicap | p | opening | 760 | no |
| premier_league | 2022/23 | match_result | avg | closing | 1140 | yes |
| premier_league | 2022/23 | match_result | avg | opening | 1140 | yes |
| premier_league | 2022/23 | match_result | b365 | closing | 1140 | no |
| premier_league | 2022/23 | match_result | b365 | opening | 1140 | no |
| premier_league | 2022/23 | match_result | bw | closing | 1140 | no |
| premier_league | 2022/23 | match_result | bw | opening | 1140 | no |
| premier_league | 2022/23 | match_result | iw | closing | 1140 | no |
| premier_league | 2022/23 | match_result | iw | opening | 1140 | no |
| premier_league | 2022/23 | match_result | max | closing | 1140 | yes |
| premier_league | 2022/23 | match_result | max | opening | 1140 | yes |
| premier_league | 2022/23 | match_result | ps | closing | 1140 | no |
| premier_league | 2022/23 | match_result | ps | opening | 1140 | no |
| premier_league | 2022/23 | match_result | vc | closing | 1140 | no |
| premier_league | 2022/23 | match_result | vc | opening | 1140 | no |
| premier_league | 2022/23 | match_result | wh | closing | 1140 | no |
| premier_league | 2022/23 | match_result | wh | opening | 1140 | no |
| premier_league | 2022/23 | total_goals | avg | closing | 760 | yes |
| premier_league | 2022/23 | total_goals | avg | opening | 760 | yes |
| premier_league | 2022/23 | total_goals | b365 | closing | 760 | no |
| premier_league | 2022/23 | total_goals | b365 | opening | 760 | no |
| premier_league | 2022/23 | total_goals | max | closing | 760 | yes |
| premier_league | 2022/23 | total_goals | max | opening | 760 | yes |
| premier_league | 2022/23 | total_goals | p | closing | 758 | no |
| premier_league | 2022/23 | total_goals | p | opening | 758 | no |
| premier_league | 2023/24 | asian_handicap | avg | closing | 760 | yes |
| premier_league | 2023/24 | asian_handicap | avg | opening | 760 | yes |
| premier_league | 2023/24 | asian_handicap | b365 | closing | 760 | no |
| premier_league | 2023/24 | asian_handicap | b365 | opening | 760 | no |
| premier_league | 2023/24 | asian_handicap | max | closing | 758 | yes |
| premier_league | 2023/24 | asian_handicap | max | opening | 760 | yes |
| premier_league | 2023/24 | asian_handicap | p | closing | 760 | no |
| premier_league | 2023/24 | asian_handicap | p | opening | 760 | no |
| premier_league | 2023/24 | match_result | avg | closing | 1140 | yes |
| premier_league | 2023/24 | match_result | avg | opening | 1140 | yes |
| premier_league | 2023/24 | match_result | b365 | closing | 1140 | no |
| premier_league | 2023/24 | match_result | b365 | opening | 1140 | no |
| premier_league | 2023/24 | match_result | bw | closing | 1104 | no |
| premier_league | 2023/24 | match_result | bw | opening | 1134 | no |
| premier_league | 2023/24 | match_result | iw | closing | 594 | no |
| premier_league | 2023/24 | match_result | iw | opening | 594 | no |
| premier_league | 2023/24 | match_result | max | closing | 1140 | yes |
| premier_league | 2023/24 | match_result | max | opening | 1140 | yes |
| premier_league | 2023/24 | match_result | ps | closing | 1140 | no |
| premier_league | 2023/24 | match_result | ps | opening | 1140 | no |
| premier_league | 2023/24 | match_result | vc | closing | 1140 | no |
| premier_league | 2023/24 | match_result | vc | opening | 1140 | no |
| premier_league | 2023/24 | match_result | wh | closing | 1140 | no |
| premier_league | 2023/24 | match_result | wh | opening | 1140 | no |
| premier_league | 2023/24 | total_goals | avg | closing | 760 | yes |
| premier_league | 2023/24 | total_goals | avg | opening | 760 | yes |
| premier_league | 2023/24 | total_goals | b365 | closing | 760 | no |
| premier_league | 2023/24 | total_goals | b365 | opening | 760 | no |
| premier_league | 2023/24 | total_goals | max | closing | 760 | yes |
| premier_league | 2023/24 | total_goals | max | opening | 760 | yes |
| premier_league | 2023/24 | total_goals | p | closing | 746 | no |
| premier_league | 2023/24 | total_goals | p | opening | 744 | no |
| premier_league | 2024/25 | asian_handicap | avg | closing | 760 | yes |
| premier_league | 2024/25 | asian_handicap | avg | opening | 760 | yes |
| premier_league | 2024/25 | asian_handicap | b365 | closing | 760 | no |
| premier_league | 2024/25 | asian_handicap | b365 | opening | 760 | no |
| premier_league | 2024/25 | asian_handicap | bfe | closing | 760 | yes |
| premier_league | 2024/25 | asian_handicap | bfe | opening | 760 | yes |
| premier_league | 2024/25 | asian_handicap | max | closing | 760 | yes |
| premier_league | 2024/25 | asian_handicap | max | opening | 760 | yes |
| premier_league | 2024/25 | asian_handicap | p | closing | 760 | no |
| premier_league | 2024/25 | asian_handicap | p | opening | 760 | no |
| premier_league | 2024/25 | match_result | avg | closing | 1140 | yes |
| premier_league | 2024/25 | match_result | avg | opening | 1140 | yes |
| premier_league | 2024/25 | match_result | b365 | closing | 1140 | no |
| premier_league | 2024/25 | match_result | b365 | opening | 1140 | no |
| premier_league | 2024/25 | match_result | bf | closing | 1140 | no |
| premier_league | 2024/25 | match_result | bf | opening | 1137 | no |
| premier_league | 2024/25 | match_result | bfe | closing | 1140 | yes |
| premier_league | 2024/25 | match_result | bfe | opening | 1140 | yes |
| premier_league | 2024/25 | match_result | bw | closing | 717 | no |
| premier_league | 2024/25 | match_result | bw | opening | 717 | no |
| premier_league | 2024/25 | match_result | max | closing | 1140 | yes |
| premier_league | 2024/25 | match_result | max | opening | 1140 | yes |
| premier_league | 2024/25 | match_result | ps | closing | 1140 | no |
| premier_league | 2024/25 | match_result | ps | opening | 1140 | no |
| premier_league | 2024/25 | match_result | wh | closing | 867 | no |
| premier_league | 2024/25 | match_result | wh | opening | 867 | no |
| premier_league | 2024/25 | total_goals | avg | closing | 760 | yes |
| premier_league | 2024/25 | total_goals | avg | opening | 760 | yes |
| premier_league | 2024/25 | total_goals | b365 | closing | 760 | no |
| premier_league | 2024/25 | total_goals | b365 | opening | 760 | no |
| premier_league | 2024/25 | total_goals | bfe | closing | 760 | yes |
| premier_league | 2024/25 | total_goals | bfe | opening | 754 | yes |
| premier_league | 2024/25 | total_goals | max | closing | 760 | yes |
| premier_league | 2024/25 | total_goals | max | opening | 760 | yes |
| premier_league | 2024/25 | total_goals | p | closing | 754 | no |
| premier_league | 2024/25 | total_goals | p | opening | 754 | no |
| premier_league | 2025/26 | asian_handicap | avg | closing | 760 | yes |
| premier_league | 2025/26 | asian_handicap | avg | opening | 760 | yes |
| premier_league | 2025/26 | asian_handicap | b365 | closing | 760 | no |
| premier_league | 2025/26 | asian_handicap | b365 | opening | 760 | no |
| premier_league | 2025/26 | asian_handicap | bfe | closing | 716 | yes |
| premier_league | 2025/26 | asian_handicap | bfe | opening | 720 | yes |
| premier_league | 2025/26 | asian_handicap | max | closing | 760 | yes |
| premier_league | 2025/26 | asian_handicap | max | opening | 760 | yes |
| premier_league | 2025/26 | asian_handicap | p | closing | 420 | no |
| premier_league | 2025/26 | asian_handicap | p | opening | 420 | no |
| premier_league | 2025/26 | match_result | avg | closing | 1140 | yes |
| premier_league | 2025/26 | match_result | avg | opening | 1140 | yes |
| premier_league | 2025/26 | match_result | b365 | closing | 1140 | no |
| premier_league | 2025/26 | match_result | b365 | opening | 1140 | no |
| premier_league | 2025/26 | match_result | bfd | closing | 1116 | no |
| premier_league | 2025/26 | match_result | bfd | opening | 1137 | no |
| premier_league | 2025/26 | match_result | bfe | closing | 1074 | yes |
| premier_league | 2025/26 | match_result | bfe | opening | 1080 | yes |
| premier_league | 2025/26 | match_result | bmgm | closing | 1140 | no |
| premier_league | 2025/26 | match_result | bmgm | opening | 1134 | no |
| premier_league | 2025/26 | match_result | bv | closing | 1116 | no |
| premier_league | 2025/26 | match_result | bv | opening | 1134 | no |
| premier_league | 2025/26 | match_result | bw | closing | 1140 | no |
| premier_league | 2025/26 | match_result | bw | opening | 1140 | no |
| premier_league | 2025/26 | match_result | cl | closing | 780 | no |
| premier_league | 2025/26 | match_result | cl | opening | 846 | no |
| premier_league | 2025/26 | match_result | lb | closing | 843 | no |
| premier_league | 2025/26 | match_result | lb | opening | 858 | no |
| premier_league | 2025/26 | match_result | max | closing | 1140 | yes |
| premier_league | 2025/26 | match_result | max | opening | 1140 | yes |
| premier_league | 2025/26 | match_result | ps | closing | 630 | no |
| premier_league | 2025/26 | match_result | ps | opening | 630 | no |
| premier_league | 2025/26 | total_goals | avg | closing | 760 | yes |
| premier_league | 2025/26 | total_goals | avg | opening | 760 | yes |
| premier_league | 2025/26 | total_goals | b365 | closing | 760 | no |
| premier_league | 2025/26 | total_goals | b365 | opening | 760 | no |
| premier_league | 2025/26 | total_goals | bfe | closing | 716 | yes |
| premier_league | 2025/26 | total_goals | bfe | opening | 720 | yes |
| premier_league | 2025/26 | total_goals | max | closing | 760 | yes |
| premier_league | 2025/26 | total_goals | max | opening | 760 | yes |
| premier_league | 2025/26 | total_goals | p | closing | 420 | no |
| premier_league | 2025/26 | total_goals | p | opening | 420 | no |

### Market coverage (repository matches)

| Competition | Season | Market | Matches | With market | Opening 1X2 | Closing 1X2 |
|---|---|---|---:|---:|---:|---:|
| premier_league | 2025/26 | match_result | 380 | 380 | 380 | 380 |
| premier_league | 2025/26 | total_goals | 380 | 380 | 0 | 0 |
| premier_league | 2025/26 | both_teams_to_score | 380 | 0 | 0 | 0 |
| premier_league | 2025/26 | corners | 380 | 0 | 0 | 0 |
| premier_league | 2025/26 | cards | 380 | 0 | 0 | 0 |
| premier_league | 2025/26 | asian_handicap | 380 | 380 | 0 | 0 |
| premier_league | 2024/25 | match_result | 380 | 380 | 380 | 380 |
| premier_league | 2024/25 | total_goals | 380 | 380 | 0 | 0 |
| premier_league | 2024/25 | both_teams_to_score | 380 | 0 | 0 | 0 |
| premier_league | 2024/25 | corners | 380 | 0 | 0 | 0 |
| premier_league | 2024/25 | cards | 380 | 0 | 0 | 0 |
| premier_league | 2024/25 | asian_handicap | 380 | 380 | 0 | 0 |
| premier_league | 2023/24 | match_result | 380 | 380 | 380 | 380 |
| premier_league | 2023/24 | total_goals | 380 | 380 | 0 | 0 |
| premier_league | 2023/24 | both_teams_to_score | 380 | 0 | 0 | 0 |
| premier_league | 2023/24 | corners | 380 | 0 | 0 | 0 |
| premier_league | 2023/24 | cards | 380 | 0 | 0 | 0 |
| premier_league | 2023/24 | asian_handicap | 380 | 380 | 0 | 0 |
| premier_league | 2022/23 | match_result | 380 | 380 | 380 | 380 |
| premier_league | 2022/23 | total_goals | 380 | 380 | 0 | 0 |
| premier_league | 2022/23 | both_teams_to_score | 380 | 0 | 0 | 0 |
| premier_league | 2022/23 | corners | 380 | 0 | 0 | 0 |
| premier_league | 2022/23 | cards | 380 | 0 | 0 | 0 |
| premier_league | 2022/23 | asian_handicap | 380 | 380 | 0 | 0 |
| premier_league | 2021/22 | match_result | 380 | 380 | 380 | 380 |
| premier_league | 2021/22 | total_goals | 380 | 380 | 0 | 0 |
| premier_league | 2021/22 | both_teams_to_score | 380 | 0 | 0 | 0 |
| premier_league | 2021/22 | corners | 380 | 0 | 0 | 0 |
| premier_league | 2021/22 | cards | 380 | 0 | 0 | 0 |
| premier_league | 2021/22 | asian_handicap | 380 | 380 | 0 | 0 |
| championship | 2025/26 | match_result | 552 | 552 | 552 | 552 |
| championship | 2025/26 | total_goals | 552 | 552 | 0 | 0 |
| championship | 2025/26 | both_teams_to_score | 552 | 0 | 0 | 0 |
| championship | 2025/26 | corners | 552 | 0 | 0 | 0 |
| championship | 2025/26 | cards | 552 | 0 | 0 | 0 |
| championship | 2025/26 | asian_handicap | 552 | 552 | 0 | 0 |
| championship | 2024/25 | match_result | 552 | 552 | 552 | 552 |
| championship | 2024/25 | total_goals | 552 | 552 | 0 | 0 |
| championship | 2024/25 | both_teams_to_score | 552 | 0 | 0 | 0 |
| championship | 2024/25 | corners | 552 | 0 | 0 | 0 |
| championship | 2024/25 | cards | 552 | 0 | 0 | 0 |
| championship | 2024/25 | asian_handicap | 552 | 552 | 0 | 0 |
| championship | 2023/24 | match_result | 552 | 552 | 552 | 552 |
| championship | 2023/24 | total_goals | 552 | 552 | 0 | 0 |
| championship | 2023/24 | both_teams_to_score | 552 | 0 | 0 | 0 |
| championship | 2023/24 | corners | 552 | 0 | 0 | 0 |
| championship | 2023/24 | cards | 552 | 0 | 0 | 0 |
| championship | 2023/24 | asian_handicap | 552 | 552 | 0 | 0 |
| championship | 2022/23 | match_result | 552 | 552 | 552 | 552 |
| championship | 2022/23 | total_goals | 552 | 552 | 0 | 0 |
| championship | 2022/23 | both_teams_to_score | 552 | 0 | 0 | 0 |
| championship | 2022/23 | corners | 552 | 0 | 0 | 0 |
| championship | 2022/23 | cards | 552 | 0 | 0 | 0 |
| championship | 2022/23 | asian_handicap | 552 | 552 | 0 | 0 |
| championship | 2021/22 | match_result | 552 | 552 | 552 | 552 |
| championship | 2021/22 | total_goals | 552 | 552 | 0 | 0 |
| championship | 2021/22 | both_teams_to_score | 552 | 0 | 0 | 0 |
| championship | 2021/22 | corners | 552 | 0 | 0 | 0 |
| championship | 2021/22 | cards | 552 | 0 | 0 | 0 |
| championship | 2021/22 | asian_handicap | 552 | 552 | 0 | 0 |

## Odds quality

| Tier | Observations | Matches | Meaning |
|---|---:|---:|---|
| A | 0 | 0 | timestamped exchange odds + liquidity |
| B | 0 | 0 | timestamped bookmaker odds |
| C | 388404 | 4660 | opening/closing odds only |
| D | 4660 | 4660 | non-odds match facts only / unavailable odds |

Timestamped-path matches across markets (must stay 0 for this source): **0**
Opening→closing same-line price pairs (implied-probability/logit): **179968**
Opening→closing line shifts (AH/line changed; not treated as price moves): **13560**

Synthetic test fixtures are separate (`docs/examples/historical-odds-coverage.md`) and are not this backfill.

The committed odds Excel workbook is **coverage/quality sheets only**. All 393064
observations (388404 quality C + 4660 facts-only D rows) remain in SQLite.
`goal_timestamps` coverage counts 0–0 matches as present because there are no goal
minutes to record; this source still does not supply event timelines.

