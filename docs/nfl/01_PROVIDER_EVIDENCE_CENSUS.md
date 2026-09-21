# NFL Phase 1 — Provider Evidence Census

**Issue:** #410  
**Parent roadmap:** #409  
**Base:** exact owner-live `588c8b41802690c226ce338e871ba17fcd367eab`  
**Mode:** PAPER MODE · EXECUTION DISABLED · GET / read-only only  
**Data class:** LIVE captured public market-data payloads from 2026-09-20 ~20:50–20:55 UTC, plus Kalshi public contract-term PDFs. Not modelled probabilities. Not demo/fixture soccer data. Prices are omitted from committed fixtures.

This is an evidence-gathering census. It does **not** implement NFL production matching, alias/equivalence registries, scanner/cadence/concurrency, settlement, or UI. Soccer behaviour is unchanged. `owner-live` is not moved.

Sanitized shape fixtures: `backend/tests/fixtures/nfl/`.

---

## 0. Method and safety

1. Branched from exact owner-live `588c8b4`.
2. Used the same public hosts already configured for Phase 1 (`KALSHI_BASE_URL`, `POLYMARKET_GAMMA_BASE_URL` / CLOB, `MATCHBOOK_BASE_URL`).
3. **GET only.** No Matchbook `POST /bpapi/rest/security/session`. No order, cancel, wallet, or signing calls.
4. Matchbook event/market/sports GETs succeeded **without credentials** in this environment. `MatchbookClient.login()` was not used. That is a capture fact, not a production-client change.
5. Did not guess missing settlement. Incomplete Matchbook rule text is recorded as unproven.

Applicable tenets: 02 (paper/read-only), 03 (do not fuzzy-match settlement), 08 (provenance), 11 (data honesty), 12 (agent review), 20 (catalogue — NFL families are **not** approved here).

---

## 1. Capture window

| Field | Value |
|---|---|
| Retrieved | 2026-09-20 ~20:50–20:55 UTC (Sunday NFL slate) |
| NFL week in Kalshi milestone | REG 2026 week 2 (IND@KC) |
| Live games observed | CLE@TB (Q4 23–19 on Polymarket), plus JAX@DEN, LV@LAC, SEA@ARI, WAS@DAL, MIA@SF |
| Completed 13:00 ET games | CAR@ATL, MIN@CHI, PIT@NE, PHI@TEN, NO@BAL, GB@NYJ, CIN@HOU |
| Upcoming same-window | IND@KC (SNF 00:20Z 21 Sep), NYG@LAR (MNF 00:15Z 22 Sep) |

---

## 2. Discovery IDs actually used

| Provider | Discovery | Native IDs |
|---|---|---|
| Kalshi | `GET /series/{ticker}`; `GET /events?series_ticker=&status=open\|settled&with_nested_markets=true&with_milestones=true` | Series `KXNFLGAME`, `KXNFLSPREAD`, `KXNFLTOTAL`, `KXNFLTEAMTOTAL`. Event ticker `KXNFLGAME-26SEP20INDKC`. Market ticker `KXNFLGAME-26SEP20INDKC-KC`. Milestone `type=football_game`. |
| Polymarket | `GET /sports` → NFL `sport=nfl`, `series=12185`, `ordering=away`; `GET /events?series_id=12185` | Event id `827222`, ticker/slug `nfl-ind-kc-2026-09-21`, market id `3482783`, `conditionId`, `clobTokenIds`, `gameId` `19484`. |
| Matchbook | `GET /edge/rest/lookups/sports` → American Football `id=1`; `GET /edge/rest/events?sport-ids=1` | Event id `33306877354500023`. Competition meta-tag NFL `491503123380010`. Market id e.g. Moneyline `33306877358600023`. Runner ids. |

`status=closed` on Kalshi `/events` returned empty for GAME/SPREAD/TOTAL in this snapshot. Graded games were under `status=settled`. Polymarket `closed=true` returned earlier (including preseason) events; the 13:00 ET Sunday games were still `closed=false` with `ended=true`.

---

## 3. Event identity

### 3.1 Same game, three native shapes — IND Colts at KC Chiefs (upcoming)

| Field | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Native event id | `KXNFLGAME-26SEP20INDKC` (also sibling `KXNFLSPREAD-…`, `KXNFLTOTAL-…`) | `827222` / `nfl-ind-kc-2026-09-21` | `33306877354500023` |
| Title | `IND Colts vs KC Chiefs` | `Colts vs. Chiefs` | `Indianapolis Colts at Kansas City Chiefs` |
| Subtitle / labels | `IND vs KC (Sep 20)` | teams[] alias `Colts`/`Chiefs`, abbr `ind`/`kc` | Full club names only |
| Home / away | Milestone `home_team_id` = KC UUID, `away_team_id` = IND UUID; title is **away vs home** | `teams[].ordering`: Colts `away`, Chiefs `home`; sport `ordering=away` | `"X at Y"` = away at home |
| Kickoff | Milestone `start_date` `2026-09-21T00:20:00Z`. Nested `occurrence_datetime` is `2026-09-21T03:20:00Z` (**not kickoff**) | `startTime` `2026-09-21T00:20:00Z`. `eventDate` is `2026-09-20` (ET calendar date) | `start` `2026-09-21T00:20:00.000Z` |
| League | `product_metadata.competition=Pro Football`; milestone `details.league=NFL` | `sport.sport=nfl`; series slug `nfl-2026` | meta-tag `NFL` type `COMPETITION` |
| Status | Event object has **no** `status` field. Markets `active`. | `period=NS`, `live/ended=null`, `closed=false`, `active=true` | `status=open`, `in-running-flag=false` |

### 3.2 Live — CLE Browns at TB Buccaneers

| Field | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Id | `KXNFLGAME-26SEP20CLETB` | `827198` / `nfl-cle-tb-2026-09-20` | `33306750563500023` |
| Lifecycle | Markets still `status=active` during the game | `live=true`, `ended=false`, `period=Q4`, `score=23-19`, `elapsed=2:00` | `status=open`, `in-running-flag=true` |
| Kickoff | Milestone `2026-09-20T17:00:00Z` | `startTime` `2026-09-20T17:00:00Z` | `start` `2026-09-20T17:02:00.000Z` (2 minutes off Polymarket/Kalshi) |

Kalshi `occurrence_datetime` / `expected_expiration_time` for this game were `2026-09-20T20:00:00Z` — three hours after kickoff. **Do not use those fields as scheduled kickoff.** Use milestone `start_date`.

### 3.3 Completed — CAR Panthers at ATL Falcons

| Field | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Result surface | `GET /events?status=settled`. Markets `status=finalized`, `result=yes` (CAR) / `no` (ATL), `expiration_value=winner` | `ended=true`, `live=false`, `period=VFT`, `score=34-3`, `finishedTimestamp` set; moneyline `closed=true`, `umaResolutionStatus=resolved`, `outcomePrices=["1","0"]` | 13:00 ET game events `status=closed`. **Moneyline/Handicap/Total were absent** from `GET .../markets?states=open,suspended,closed,graded` (only player props remained on MIN@CHI / PIT@NE). |

Matchbook GET event still returns identity + `status=closed` after the game. Graded moneyline exact-ID refresh was **not** proven: the moneyline market id was not listed after close, and no score/result fields appear on the event object.

---

## 4. Market identity

### 4.1 Game winner / moneyline (full game)

All three offer a **two-team winner**, not soccer 1X2.

| | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Family | `KXNFLGAME` series; `product_metadata.competition_scope=Game` | `sportsMarketType=moneyline` | `name=Moneyline`, `market-type=money_line`, `type=binary` |
| Market id | `KXNFLGAME-26SEP20INDKC-KC` / `-IND` | Gamma `3482783`; CLOB token ids | `33306877358600023` |
| Sides | Two YES contracts, `mutually_exclusive=true`. Titles `Kansas City wins` / `Indianapolis wins`. **No Tie strike.** | Outcomes `["Colts","Chiefs"]`. **No Draw.** | Runners `Indianapolis Colts`, `Kansas City Chiefs`. **No Draw.** `number-of-winners=1` |
| Period | Contract terms default: full regulation **including overtime** | Description: “If X wins”; no explicit OT sentence | No rule text on payload |
| Price | Binary dollar yes/no (`yes_bid_dollars`) | `outcomePrices` + CLOB `/book?token_id=` | Pregame: back/lay DECIMAL when requested. In-running live moneyline used `exchange-type=binary` with `side=win/lose` (observed on CLE@TB) |
| Parent/child | Sibling series share milestone `main_game_event_ticker` | Many child markets on one Gamma event | Markets listed under one event id |

### 4.2 Point spread

| | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Shape | Ladder of **binary win-margin** contracts, one event, `mutually_exclusive=false`. Example: `Kansas City wins by over 6.5 points?` ticker `…-KC7` | One Gamma market per line: `Spread: Chiefs (-6.5)`, `line=-6.5`, outcomes `[Chiefs, Colts]`, slug `…-spread-home-6pt5` | One market per line: `name=Handicap`, `market-type=handicap`, market-level `handicap=6.5`, runners `Indianapolis Colts +6.5` / `Kansas City Chiefs -6.5` |
| Sign | “Team wins by over X.5” = covering that team **−(X.5)** | Home-centric question `Chiefs (-6.5)`; `line` is signed on the named (home) team | Runner `handicap` is signed per side; away plus / home minus in this sample |
| Integer vs half | Observed **half-lines only** (1.5, 2.5, … 27.5) | Observed **half-lines** in this snapshot (`-1.5`, `-3.5`, `-6.5`, …) | **Both** integer (`+5.0` / `−5.0`) and half (`+4.5` / `−4.5`) |
| Tie / push | Contract PDF: if tied, point differential is 0 and any “won by” positive margin → No. Half-line therefore has **no push**. | Tie → **underdog/away side** of that market (“resolve to Colts”), **not** 50-50 and **not** a void. Cancel → 50-50. | No rule text. Integer handicap is a push candidate by American-football convention; **unproven on Matchbook.** |

Kalshi `KXNFLSPREAD-26SEP20INDKC-KC7` (“wins by over 6.5”) is the economic cover of Polymarket `Chiefs (-6.5)` and Matchbook `Chiefs -6.5` **only if** overtime and tie handling match. See §8.

### 4.3 Game total points

| | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Shape | Binary per threshold: `Full Game: over 47.5 points scored?` ticker `KXNFLTOTAL-…-48`. YES=over, NO=under | `Colts vs. Chiefs: O/U 47.5`, outcomes `Over`/`Under`, `line=47.5` | `name=Total`, `market-type=total`, runners `OVER 47.5` / `UNDER 47.5`, `handicap=47.5` |
| Lines | Half-lines only in this snapshot (27.5 … 69.5) | Half-lines in sampled markets | **Both** integer (`OVER 46.0`) and half (`OVER 45.5`) |
| OT | FOOTBALLTOTALS.pdf: regulation **and overtime** count unless period specified | Description counts “this game” combined score; no explicit OT sentence | No rule text |
| Push | Half-line → no push. Integer totals **not offered** on Kalshi here | Half-line → no push in sampled text | Integer totals exist → push possible by convention; **unproven** |

Live CLE@TB Kalshi totals had already `finalized` the low thresholds (e.g. over 19.5 `result=yes`, `expiration_value="22"`) while the game was still in Q4 — early close when the criterion is already satisfied.

### 4.4 Team total (evidence only — not approved)

Present on Kalshi (`KXNFLTEAMTOTAL`, named team + over X.5) and Polymarket (`sportsMarketType=team_totals`, `Panthers Team Total: O/U 10.5`). **Not observed** as a Matchbook full-game team-total market on IND@KC / NYG@LAR / CLE@TB (Matchbook `Total` runners were game over/under only). Do not treat as Phase-2/3 approved.

---

## 5. Cross-provider concept matrix (IND@KC unless noted)

| Concept | Kalshi | Polymarket | Matchbook | Same economics? |
|---|---|---|---|---|
| Full-game winner, two teams, OT included, tie splits 50/50 | Proven (rules + PDF) | Strongly indicated (“If X wins”; tie → 50-50). OT not named. | Two-way Moneyline **observed**. OT/tie/void **not in payload**. | Kalshi↔Polymarket **plausible but OT not independently proven on PM**. Matchbook **unsupported** until rules exist. **Not soccer 1X2.** |
| Spread −6.5 home | `wins by over 6.5` YES | `Spread: Chiefs (-6.5)` | `Chiefs -6.5` / `Colts +6.5` | Line match is possible. **Tie handling differs on Polymarket** (tie → Colts, not void). Kalshi half-line tie → No. Matchbook integer/half push unknown. |
| Total 47.5 | `over 47.5` YES/NO | `O/U 47.5` Over/Under | `OVER/UNDER 47.5` | Half-line O/U is the closest shared family **if** OT inclusion matches. |
| Integer total 46.0 | **Not listed** | Not sampled | Present | Kalshi unavailable → cannot pair. |
| Draw / tie runner | **Absent** on GAME (tie pays $0.50 on each team strike) | **Absent** (50-50) | **Absent** on every NFL Moneyline sampled | None expose a soccer-style Draw runner. |
| 1st half / quarter ML, spreads, totals | Separate series (`KXNFL1H`, `KXNFL1Q`, …, `KXNFLOT`) | `first_half_*`, `q1_*`, … | `1st Half Moneyline/Handicap/Total` | **Not** full-game equivalent. |
| Player props / first TD / exact margin | Many related tickers on the milestone | `exact_margin`, `safety`, player-adjacent types | Separate events `… Player Props BT` + `First Touchdown Scorer` | Unsafe for initial equivalence. |

---

## 6. Exceptional lifecycle

| Case | Kalshi (rules + PDF + nested `rules_secondary`) | Polymarket (market `description`) | Matchbook (payload) |
|---|---|---|---|
| Tie after OT | Each team strike → **$0.50**. No Tie contract on sampled GAME events. PDF example: 20–20 after OT. | Moneyline: **50-50**. Spread: **not** 50-50 — the underdog/away outcome wins the spread market. | Not exposed. Binary `number-of-winners=1`. |
| Push | Half-line spread/total: no push. Integer spread/total not seen on Kalshi NFL. | Half-line totals: over iff `line+0.5` or more. Spreads: tie goes to the non-favorite named in the “otherwise” clause. | Integer handicap/total exist; push **unproven**. |
| Postponed | If starts within **48 hours** of original start: remain open, settle on official result. Else **fair price**. | Remain open “until the game has been completed”. Cancel with no make-up → 50-50. **No 48h clause.** | Not exposed. |
| Cancelled | Fair price if not started within 48h. | 50-50 if canceled entirely with no make-up. | Not exposed. |
| Suspended / abandoned | PDF: before 55 minutes of play and not resumed within 48h → fair price; after 55 minutes or league declares final → settle on what occurred. | Not named beyond postpone/cancel. | `status` values observed: `open`, `closed`. `suspended` was requested but not seen on this slate. |
| Void | Not a separate status; fair-price / $0.50 paths above. | 50-50 used as the cancel/tie moneyline path. | Not exposed. |
| Graded result fields | Market `status=finalized`, `result=yes\|no`, `expiration_value` (`winner` or a point total like `"22"`). Event-level status field absent. | `umaResolutionStatus=resolved`, `outcomePrices` 1/0, event `ended`/`score`/`period=VFT`/`finishedTimestamp`. | Event `status=closed`. No score. Full-game markets dropped from the list on sampled closed events. |

No postponed, cancelled, or tied NFL game was **in the live snapshot**. Those rows are from current contract text, not from a captured exceptional event.

---

## 7. Refresh architecture (exact IDs after discovery)

Persist these native IDs; do not rediscover by label.

**Kalshi**

- Persist `series_ticker` + `event_ticker` + market `ticker` (and milestone `id` if kickoff is required).
- Cheap refresh: `GET /events/{event_ticker}?with_nested_markets=true` (proven for `KXNFLGAME-26SEP20INDKC`); `GET /markets/{ticker}` (proven for `KXNFLGAME-26SEP20INDKC-KC`); `GET /markets/{ticker}/orderbook` already used in soccer Phase 1.
- Kickoff refresh: milestone `start_date` via `with_milestones=true` on list-events, not `occurrence_datetime`.
- Sibling GAME/SPREAD/TOTAL/TEAMTOTAL events are **different event tickers** sharing a milestone.

**Polymarket**

- Persist Gamma event `id` and market `id`, plus `conditionId` and both `clobTokenIds`.
- Cheap refresh: `GET /events/{id}` (proven `827198`, `827222`); `GET /markets/{id}` (proven `3482783`); CLOB `GET /book?token_id=` (proven).
- `gameId` is on the event, not on `GET /markets/{id}`.
- Series discovery remains `GET /events?series_id=12185`.

**Matchbook**

- Persist numeric `event id`, `market id`, `runner id` (and `event-participant-id` if home/away binding is needed).
- Cheap refresh: `GET /edge/rest/events/{id}`; `GET /edge/rest/events/{id}/markets/{market_id}` (proven for IND@KC moneyline). Existing soccer client also has runner prices.
- Filter discovery with `sport-ids=1` and NFL meta-tag `491503123380010`. Player-prop **events** are separate ids and must not be treated as the game event.
- After `status=closed`, full-game market ids were **not** returned by list-markets in this capture. Do not assume the moneyline id remains listable; exact GET-by-id after close is unproven.

---

## 8. Answers to the eight #410 questions

1. **Can all three represent the same NFL game-winner semantics?**  
   All three expose a **two-team winner** with **no Draw runner**. That is **not** soccer 1X2. Kalshi explicitly includes overtime and splits a final tie 50/50. Polymarket moneyline text matches the tie split and “if team wins” language but **does not mention overtime**; treat OT inclusion as **not independently proven**. Matchbook shows the two-team book and nothing else. **Kalshi↔Polymarket is not proven equivalent. Matchbook is unsupported for settlement pairing.** Do not reuse the soccer GAMEWIN-with-Tie assembler.

2. **How does each encode totals and spreads (sign/side/line)?**  
   - **Kalshi spread:** one binary per team+threshold, “{team} wins by over {N.5} points?”. Covering team −N.5 is the YES of that contract.  
   - **Polymarket spread:** `line` signed on the named team (usually home favorite), outcomes `[favorite, other]`. Tie is assigned to the “otherwise” side, not voided.  
   - **Matchbook spread:** `Handicap` market, per-runner signed `handicap`, names embed `+6.5` / `-6.5`.  
   - **Kalshi total:** YES = combined score > N.5.  
   - **Polymarket total:** Over if combined score ≥ ceil(line).  
   - **Matchbook total:** `OVER`/`UNDER` runners sharing the same `handicap` line, including integers.

3. **Is overtime included for each relevant full-game market?**  
   **Kalshi yes** (GAME/SPREAD/TOTAL PDFs: regulation + OT unless period specified). Separate `KXNFLOT` series is overtime-only and is **not** the full-game market. **Polymarket not stated** in sampled descriptions. **Matchbook not stated.** Fail closed for PM/MB OT until proven.

4. **Ties, pushes, voids, cancelled/postponed?**  
   See §6. No live tie/cancel/postpone instance was captured. Do not invent Matchbook behaviour from soccer `_standard_football_settlement`.

5. **Does Matchbook expose a draw/tie runner for NFL game winner?**  
   **No** on sampled NFL Moneyline (live CLE@TB, upcoming IND@KC, upcoming NYG@LAR) and **no** on `1st Half Moneyline`. Two club-named runners only.

6. **Are provider home/away labels reliable enough to use directly?**  
   **No.** Use canonical team identity. Evidence: Polymarket `ordering=away` titles “Away vs Home”; Matchbook “Away at Home”; Kalshi title “Away vs Home” **plus** milestone UUID home/away which is the structured signal. Ambiguous labels: Kalshi `Los Angeles R` / `New York G`, Polymarket slug `nfl-nyg-la-2026-09-22` vs Kalshi `NYGLAR`. Kickoff clocks disagree by two minutes on Matchbook vs the others. `eventDate` / subtitle calendar dates are ET-ish, not UTC.

7. **Exact native IDs to persist for cheap refresh?**  
   See §7. Minimum: Kalshi market `ticker`; Polymarket market `id` + both CLOB token ids; Matchbook `event id` + `market id` (+ runner ids for books).

8. **Which observed labels are clearly not safe for initial equivalence?**  
   Anything not full-game two-way winner / not the same spread line / not the same total line, including: `KXNFLOT` and all `1H`/`2H`/`Qn` series; Polymarket `first_half_*`, `q1_*`, `exact_margin`, `safety`, `longest_field_goal`, `two_point_conversions`; Matchbook `1st Half *`, `First Touchdown Scorer`, `… Player Props BT` events; Kalshi `KXNFLGAMEFG`, `KXNFLWINS-*`, draft/coach/viewership series; team totals until an NFL catalogue says otherwise; soccer 1X2 / BTTS / FTTS recognisers; Kalshi GAMEWIN **with Tie strike** (soccer) vs NFL GAME **without**.

---

## 9. Gaps / ambiguities (must not be guessed in Phase 2)

- Matchbook NFL settlement (OT, tie, integer push, postpone, 55-minute rule) has **no payload wording** and **no graded moneyline sample**.
- Polymarket moneyline/totals **omit the word overtime**.
- Polymarket spread **tie ≠ moneyline tie** (underdog wins the spread market). Pairing that to Kalshi “wins by over X.5” or Matchbook Asian-style handicap needs an explicit Phase 3 decision.
- Kalshi `occurrence_datetime` is not kickoff.
- Kalshi event objects lack a status field; use market `status` / list filter `open` vs `settled`.
- No captured postponed/cancelled/tied game.
- Unauthenticated Matchbook GETs worked here; production `MatchbookClient` still requires login. This census does not change that client.
- Live Matchbook moneyline used `exchange-type=binary` rather than back/lay. Pricing adapters must not assume soccer back/lay for NFL in-running books.
- Team totals: Kalshi + Polymarket only in this capture.

---

## 10. What this census does not do

- No NFL production code, alias registry, market equivalence registry, matcher, scanner, cadence, concurrency, settlement, or UI changes.
- No soccer behaviour change.
- No `owner-live` move.
- No provider writes.
- No claim that NFL families are `APPROVED_EQUIVALENT` or paper-assumed.

Phase 1 is complete when Phase 2 canonical identity rules and Phase 3 approved equivalences can be decided **from this evidence without guessing**. Residual unproven cells are listed in §9 so they stay fail-closed.

---

## 11. Fixture index

All under `backend/tests/fixtures/nfl/`. Shape evidence only; prices stripped.

| File | Proves |
|---|---|
| `kalshi_series_kxnflgame.json` (and SPREAD/TOTAL/TEAMTOTAL) | Series tickers + contract-terms URLs |
| `kalshi_event_game_indkc_open.json` | Upcoming two-way GAME, tie $0.50 wording |
| `kalshi_event_game_cletb_open.json` | In-progress GAME still `active` |
| `kalshi_event_game_caratl_settled.json` | `finalized` / `result` / `expiration_value` |
| `kalshi_milestone_indkc_kickoff.json` | Kickoff `start_date`, home/away UUIDs |
| `kalshi_market_spread_indkc_kc7.json` | Win-margin ladder line |
| `kalshi_market_total_indkc_48.json` | Full-game over 47.5 |
| `polymarket_sports_nfl.json` | series `12185`, `ordering=away` |
| `polymarket_event_indkc_upcoming.json` | Identity + ML/spread/total descriptions |
| `polymarket_event_cletb_live.json` | Live period/score |
| `polymarket_event_caratl_ended.json` | Ended VFT + resolved moneyline |
| `matchbook_lookups_sports_american_football.json` | sport-id `1` |
| `matchbook_event_indkc_upcoming.json` | Two-way ML, signed handicap, O/U |
| `matchbook_event_cletb_live.json` | In-running ML |
| `matchbook_event_nyglar_upcoming.json` | Second upcoming game |
| `matchbook_event_minchi_closed.json` | Closed event; full-game markets gone |

---

## 12. Tenet review

```text
Applicable tenets:
- 02 paper/read-only venue boundary
- 03 canonical market equivalence (fail closed; no NFL pairing implemented)
- 08 provenance of captured payloads
- 11 data honesty (live captured vs unproven)
- 12 agent review contract
- 20 approved catalogue (NFL not added)

Satisfied:
- 02: GET-only; no execution; no production venue writes
- 03: uncertain pairings marked unsupported
- 08: source URL + retrieved_at on fixtures
- 11: live captured public data, not demo
- 20: no silent NFL catalogue approval

Partial / deferred:
- Matchbook NFL settlement proof
- Polymarket explicit overtime wording
- Exceptional (tie/cancel/postpone) live instances

Potential conflicts:
- none. NFL is not wired into soccer scanners.

Data honesty:
- live captured 2026-09-20 public payloads + public Kalshi PDFs
- committed fixtures are sanitized shapes, not quote streams

Safety:
- paper-only boundary preserved; no production behaviour changed
```
