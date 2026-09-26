# MLB Stage 1 provider evidence

Census date: 2026-09-24. Read-only public GETs and Kalshi contract PDFs. No orders, no authenticated trading calls, and no invented series IDs.

Stage-1 candidate families are Game Winner / Moneyline and Total Runs x.5. Run line, first-five innings, inning markets, player props, series markets, and futures stay out of the executable catalogue.

## Coverage

| Venue | What was observed | Stage-1 use |
| --- | --- | --- |
| Kalshi | `KXMLBGAME` and `KXMLBTOTAL` on open MLB games. Sample event `KXMLBGAME-26SEP241235STLPIT` (“St. Louis vs Pittsburgh”), milestone `start_date` `2026-09-24T16:35:00Z`, `type=baseball_game`, `details.league=MLB`. Home/away come from `home_team_id` / `away_team_id` plus `custom_strike.baseball_team`. `occurrence_datetime` is not first pitch. | Game and total series only. |
| Kalshi, not admitted | `KXMLBSPREAD` (“wins by over N.5 runs”), first-five and other `KXMLB*` heads (wins, awards, World Series). Short prefix `KXMLB` is too wide. | Rejected. Soccer suffix matching must not claim `KXMLBGAME` or `KXMLBTOTAL`. |
| Polymarket Gamma | `sport=mlb`, series id exactly `3`, `ordering=away`. Slug shape `mlb-stl-pit-2026-09-24` is date-only. `sportsMarketType` values seen include `moneyline`, `totals`, `spreads`, `nrfi`, first-five spread/total, and `baseball_game_extra_innings`. | Moneyline and totals only, and only with real CLOB token IDs. |
| Polymarket, not MLB | `kbo` 10370, `npb` 11968, `wbc` 11249, `ncaabaseball` 12791, `cuba` 11971. `mlbb` is esports. Player-prop slugs are separate events. | Fail closed. |
| Matchbook | Sport id `3` name Baseball. Competition tag `1494669213760003` “Major League Baseball”. Sample event `34422443059100081` “St. Louis Cardinals at Pittsburgh Pirates”, start `2026-09-24T16:35:00.000Z`. Markets: two-runner Moneyline, Total handicap `5.5` / `6.5` / `7.5`, and run-line handicap `±1.5`. | Moneyline and exact x.5 totals only. “X at Y” is away at home. |
| Matchbook, not a fixture | “MLB World Series 2026” sits on the same competition tag. | Rejected as an outright. |

Fees stay venue/account-specific. `KXMLBGAME` reported `fee_type=quadratic_with_maker_fees` and `fee_multiplier=0.5` on the census. That is not a sport fee table.

## Team labels

Observed labels that resolve: Kalshi short forms (`Chicago WS` / `CWS`, `Chicago C` / `CHC`, `New York M` / `NYM`, `New York Y` / `NYY`, `Los Angeles A` / `LAA`, `Los Angeles D` / `LAD`, `A's` / `ATH`, `AZ`, `TB`, `WSH`) and Matchbook full names, including Athletics rather than Oakland.

Ambiguous labels fail closed: Chicago, New York, NY, Los Angeles, LA, C, WS.

Historical names are rejected: Cleveland Indians, Florida Marlins, Montreal Expos, California/Anaheim Angels, Oakland Athletics, Tampa Bay Devil Rays.

## Doubleheaders

No doubleheader was on the 41 open Kalshi games that day. Kalshi tickers embed an ET start (`1235` in the sample). Contract text allows the exchange to name a game number in a doubleheader. Polymarket slugs are date-only, so date plus clubs is not identity.

Sports Hedge stores a scheduled game key: UTC start truncated to the minute, plus `|game-N` when the provider text says Game 1 or Game 2. Missing or unequal keys do not cluster. Same clubs and the same calendar date are not enough.

## Settlement

The 2026-09-24 census recorded wording differences across venues. On 2026-09-26 the owner independently reviewed that evidence and approved PAPER venue-pair comparison for structurally valid Game Winner and exact x.5 Total Runs markets. That approval does not add a new settlement model, and it does not admit any other family.

Kalshi `BASEBALLGAMEWIN` rules, from the public contract PDF:

- Extra innings are included.
- A tie without a Tie strike pays $0.50 on each side.
- A shortened official game settles on the official result.
- A postponement, suspension, or cancellation that is not started or resumed within 48 hours settles at the last fair price.
- The sampled full-game `rules_primary` and PDF do not state a listed-pitcher clause.

Kalshi `BASEBALLTOTALS` rules:

- Extra innings (“overtime” in the PDF) count.
- The contract is greater-than an x.5 line (“Over N.5 runs scored”).
- Forfeit, shortened-game, and 48-hour fair-price paths are stated.

Polymarket moneyline text on series 3:

- Settles if the named team wins.
- A postponement stays open until the game is completed. There is no 48-hour cutoff.
- A cancellation with no makeup, or a tie, is 50-50.
- Extra innings are not named.

Polymarket totals:

- Over wins if combined runs are at least line + 0.5, which is the same arithmetic as over x.5 on a half-run line.
- Postponement and cancellation wording matches the moneyline.
- Extra innings are not named.

Matchbook market payloads for the sample game have runners and handicaps and no settlement wording.

Census differences the owner reviewed before approving PAPER comparison:

- Kalshi’s 48-hour last-fair-price path versus Polymarket’s open-ended postponement and 50-50 cancel.
- Extra innings are explicit only on Kalshi.
- Matchbook settlement text is absent.
- A pitcher-dependent market, if one appears later, is not equated with an action market.

Whole-number totals are not modelled. A line mismatch stays rejected with `mlb_total_line_mismatch`. A pair that fails the structural register stays `mlb_structural_identity_not_admitted`. The historical string `mlb_settlement_equivalence_not_proven` is not a PAPER blocker. Structurally valid Game Winner and same-line x.5 Total Runs pairs receive `MLB_GAME_WINNER_FT` or `MLB_TOTAL_RUNS_FT:<line>` and follow the existing PAPER catalogue path. They are not live-execution eligible.

## What Stage 1 admits

Discovery, curated team identity, and fixture identity (including doubleheader keys) stay as recorded in this census. Game Winner and Total Runs x.5 are the only PAPER families. Run line, first five, inning markets, props, and futures stay out. The operator catalogue marks MLB `paper_executable` for those registered families. UNIVERSE can catalogue a scan-eligible pair, and an ACTIVE row is repriced by its stored native IDs. Automatic paper settlement of those families uses the graded score when it determines a canonical outcome. `mlb_settlement_equivalence_not_proven` is not a PAPER blocker for them. A whole-number total, a tied two-way winner, or a score that does not determine the outcome stays fail-closed.
