# NBA Phase 1 — Provider Evidence Census

**Issue:** #449  
**Parent roadmap:** #448  
**Base:** exact owner-live `588c8b41802690c226ce338e871ba17fcd367eab`  
**Mode:** PAPER MODE · EXECUTION DISABLED · GET / read-only only  
**Data class:** LIVE captured public market-data payloads from 2026-09-21 ~08:32–08:40 UTC, plus Kalshi public contract-term PDFs and Polymarket US public Sports FAQs. Not modelled probabilities. Not demo/fixture soccer or NFL data. Prices are omitted from committed fixtures.

This is an evidence-gathering census. It does **not** implement NBA production matching, alias/equivalence registries, scanner/cadence/concurrency, settlement, or UI. Soccer and NFL behaviour are unchanged. `owner-live` is not moved.

Sanitized shape fixtures: `backend/tests/fixtures/nba/`.

---

## 0. Method and safety

1. Branched from exact owner-live `588c8b4`.
2. Used the same public hosts already configured for Phase 1 (`KALSHI_BASE_URL`, `POLYMARKET_GAMMA_BASE_URL` / CLOB, `MATCHBOOK_BASE_URL`).
3. **GET only.** No Matchbook `POST /bpapi/rest/security/session`. No order, cancel, wallet, or signing calls.
4. Matchbook event/market/sports GETs succeeded **without credentials** in this environment. `MatchbookClient.login()` was not used. That is a capture fact, not a production-client change.
5. Did not guess missing settlement. Incomplete Matchbook NBA game rule text and absent NBA game books are recorded as unproven.
6. This capture is **off-season** relative to NBA 2026-27 (opening night 20 Oct 2026). Upcoming Kalshi GAME winners exist; Polymarket and Matchbook had **no listed NBA game books** in the window. Historical/graded Polymarket games and Kalshi settled event identities were sampled instead of inventing live games.

Applicable tenets: 02 (paper/read-only), 03 (do not fuzzy-match settlement), 08 (provenance), 11 (data honesty), 12 (agent review), 20 (catalogue — NBA families are **not** approved here).

---

## 1. Capture window

| Field | Value |
|---|---|
| Retrieved | 2026-09-21 ~08:32–08:40 UTC (Monday; NBA regular season not started) |
| Upcoming Kalshi GAME | BOS@DET, PHI@NYK, OKC@SAS — opening night 20–21 Oct 2026 |
| Upcoming Kalshi SPREAD/TOTAL | **none open** |
| Polymarket series `10345` open games | **none** (`closed=false` empty; historical closed events exist) |
| Matchbook NBA competition tag | only `NBA Championship Winner 2026/27` outright; **no NBA game events** |
| Historical / graded samples | Kalshi settled Finals/play-in identities (markets archived); Polymarket Finals G5 NYK@SAS 13 Jun 2026 (`period=VFT`, `score=94-90`); Clippers@Lakers; Nets@Knicks |
| Live NBA games | **none** in this snapshot |

---

## 2. Discovery IDs actually used

| Provider | Discovery | Native IDs |
|---|---|---|
| Kalshi | `GET /series/{ticker}`; `GET /events?series_ticker=&status=open\|settled&with_nested_markets=true&with_milestones=true` | Series `KXNBAGAME`, `KXNBASPREAD`, `KXNBATOTAL`, `KXNBATEAMTOTAL`. Event ticker `KXNBAGAME-26OCT20BOSDET`. Market ticker `KXNBAGAME-26OCT20BOSDET-BOS`. Milestone `type=basketball_game`. |
| Polymarket | `GET /sports` → NBA `sport=nba`, `series=10345`, `ordering=away`; `GET /events?series_id=10345`; `GET /public-search` for historical games | Event id `567958`, ticker/slug `nba-nyk-sas-2026-06-13`, market id `2459554`, `conditionId`, `clobTokenIds`, `gameId` `20023822`. |
| Matchbook | `GET /edge/rest/lookups/sports` → Basketball `id=4`; `GET /edge/rest/events?sport-ids=4`; `GET /edge/rest/events?tag-ids=406202315670010` | NBA competition meta-tag `406202315670010`. Only listed NBA event: championship `33613052933500045`. |

`status=closed` on Kalshi `/events` returned empty for GAME. Graded games were under `status=settled` **as event shells** (nested markets empty). Guessed settled market tickers (`…-NYK`, `…-SAS`) returned HTTP 404. Polymarket `closed=true` on series `10345` returned 2025 preseason first (oldest-first). Matchbook `states=open,suspended,closed,graded` still showed no NBA game events.

---

## 3. Event identity

### 3.1 Same upcoming game — BOS Celtics at DET Pistons (Kalshi only in this window)

| Field | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Native event id | `KXNBAGAME-26OCT20BOSDET` | **not listed** (`/events?slug=nba-bos-det-2026-10-20` empty; search did not surface opening night 2026) | **not listed** |
| Title | `Boston vs Detroit` | — | — |
| Subtitle / labels | `BOS vs DET (Oct 20)` | — | — |
| Home / away | Milestone title `Boston at Detroit`; `home_team_id` DET UUID `65569528-…`, `away_team_id` BOS UUID `6dd427f5-…`; event title is **away vs home** | — | — |
| Tipoff | Milestone `start_date` `2026-10-20T19:00:00Z`. Nested `occurrence_datetime` is `2026-10-20T22:00:00Z` (**not tipoff**) | — | — |
| League | `product_metadata.competition=Pro Basketball (M)`; milestone `details.league=NBA`; `details.status=scheduled` | sport object `nba` / series `10345` exists, no event yet | NBA tag exists on championship outright only |
| Status | Event object has **no** `status` field. Markets `active`. | — | — |

Kalshi `occurrence_datetime` / `expected_expiration_time` are three hours after milestone tipoff. **Do not use those fields as scheduled tipoff.** Use milestone `start_date`. Same pattern as NFL Phase 1.

Sibling SPREAD/TOTAL event tickers were **not** on `related_event_tickers` yet (only GAME). Spreads/totals are not listed for opening night in this snapshot.

### 3.2 Completed — NYK Knicks at SAS Spurs (2026 Finals G5)

| Field | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Id | Event shell `KXNBAGAME-26JUN13NYKSAS` title `Game 5: New York at San Antonio` / `NYK at SAS (Jun 13)` | `567958` / `nba-nyk-sas-2026-06-13` | **not listed** |
| Result surface | Event identity only. **No nested markets.** Exact `GET /markets/{guessed ticker}` → 404. No `result` / `expiration_value`. | `ended=true`, `live=false`, `period=VFT`, `score=94-90`, `finishedTimestamp` set; moneyline `closed=true`, `umaResolutionStatus=resolved` | — |
| Kickoff | Not on the settled event object (no milestone in the settled list payload used here) | `startTime` `2026-06-14T00:30:00Z`; `eventDate` `2026-06-13` (ET calendar date) | — |

### 3.3 Ambiguous labels — Los Angeles and New York

Canonical team identity **must** drive ordering. Evidence:

| Club | Kalshi | Polymarket |
|---|---|---|
| Lakers | Title `Los Angeles L`; ticker `LAL` (e.g. `KXNBAGAME-26MAY11OKCLAL`) | alias/name `Lakers`, abbr `lal`, team id `100515` |
| Clippers | Title `Los Angeles C`; ticker `LAC` (e.g. `KXNBAGAME-26APR15GSWLAC` “Golden State at Los Angeles C”) | alias/name `Clippers`, abbr `lac`, team id `100514` |
| Knicks | Title often city `New York` (upcoming `Philadelphia vs New York`, ticker `NYK`; settled `New York at Brooklyn`) | alias/name `Knicks`, abbr `nyk`, team id `100494` |
| Nets | Title `Brooklyn`; ticker `BKN` (e.g. `KXNBAGAME-26MAR20NYKBKN`) | alias/name `Nets`, abbr `bkn`, team id `100512` |

Kalshi city truncation (`Los Angeles L` / `Los Angeles C`, `New York` without Knicks) is **not** a safe join key. Polymarket nicknames+abbreviations disambiguate these four clubs in sampled events. Matchbook NBA game labels were not observed; WNBA `New York Liberty` is a further city-name collision if anyone used “New York” alone.

Do not implement aliases in this PR.

---

## 4. Market identity

### 4.1 Game winner / moneyline (full game) — candidate Stage-1 family

All observed NBA/basketball game winners are **two-team**, not soccer 1X2.

| | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Family | `KXNBAGAME`; `product_metadata.competition_scope=Game` | `sportsMarketType=moneyline` | **NBA game Moneyline not listed.** Championship is `market-type=outright` (unsafe). WNBA analogue: `name=Moneyline`, `market-type=money_line`, `type=binary` |
| Market id | `KXNBAGAME-26OCT20BOSDET-BOS` / `-DET` | Gamma `2459554`; CLOB token ids | Championship market `33613098386100045`. WNBA ML `34398301286100081` is **not NBA** |
| Sides | Two YES contracts, `mutually_exclusive=true`. Titles `Boston wins` / `Detroit wins`. **No Tie strike.** `custom_strike.basketball_team` UUID | Outcomes `["Knicks","Spurs"]`. **No Draw.** | WNBA analogue: two club-named runners, `number-of-winners=1`, **no Draw** |
| Period | Series PDF default: regulation **including overtime**. Market `rules_primary/secondary` do **not** say “overtime” | Description: “final score including any overtime periods” | No NBA game rule text on payload |
| Price | Binary dollar yes/no (stripped) | `outcomePrices` + CLOB `/book?token_id=` (closed token: no orderbook) | Pregame WNBA analogue used `exchange-type=binary` with `side=win/lose` |
| Parent/child | GAME vs SPREAD/TOTAL are **different event tickers** when listed; not yet related for opening night | Many child markets on one Gamma event | Markets listed under one event id |

### 4.2 Point spread — candidate Stage-1 family (not listed live on Kalshi/PM/MB NBA in this window)

| | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Shape | Series `KXNBASPREAD` exists. **Zero open events.** Settled event shells have no markets. PDF: “points differential in favour of `<team>` … `<above/below/exactly/at least/between>` `<count>`” | Historical: one Gamma market per line, e.g. `Spread: Spurs (-5.5)`, signed `line=-5.5`, outcomes `[Spurs, Knicks]`, slug `…-spread-home-5pt5` | NBA game Handicap **not listed**. WNBA analogue: `name=Handicap`, `market-type=handicap`, market-level `handicap`, runners `Atlanta Dream +2.0` / `New York Liberty -2.0` |
| Sign | PDF: differential = team minus opponent; losing is negative; tie is 0 | Named (usually favorite) side in the question; `line` signed on that side. Cover = win by `ceil(|line|)` | Per-runner signed `handicap` |
| Integer vs half | PDF allows whole or half. **No live NBA ladder sampled** | Finals G5: **half-lines only** (41 spreads, 0 integer) | WNBA analogue: **both** integer (`+2.0`) and half (`+2.5`) |
| Tie / push | Integer differential **can** equal `<count>` (exact). Half-line “above 6.5” has no game-score push. PDF does not say void; exact operator would resolve Yes/No on equality | Tie of the **game** (not the line) is assigned to the **other** outcome (“If the game ends in a tie, this market will resolve to Knicks”), **not** 50-50 and **not** a void. Half-line cover has no push at `n.5`. Cancel → 50-50 in the market text | Integer handicap is a push candidate by basketball convention; **unproven on Matchbook NBA** (no rule text, no NBA sample) |

Kalshi `KXNBASPREAD` economics can match Polymarket `Spurs (-5.5)` **only if** overtime, push, and cancel paths match. See §6 and §8. **Not proven in this capture** because Kalshi had no live/graded spread markets.

### 4.3 Game total points — candidate Stage-1 family

| | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Shape | Series `KXNBATOTAL`. **Zero open events.** PDF: combined points in `<time period>` vs `<count>` with above/below/at-least operators | Historical: `Knicks vs. Spurs: O/U 218.5`, outcomes `Over`/`Under`, `line=218.5`. Over if combined ≥ ceil(line) (219 for 218.5) | NBA game Total **not listed**. WNBA analogue: `name=Total`, `market-type=total`, runners `OVER 162.5` / `UNDER 162.5` |
| Lines | PDF allows whole or half. None live | Finals G5: **half-lines only** (25 totals, 0 integer) | WNBA analogue sampled totals were half-lines; integer totals not seen on that event |
| OT | PDF: regulation **and overtime** unless period specified | Sports FAQ: Totals include OT. **This market description does not say “overtime”** (only “in this game”) | Unproven for NBA |
| Push | Half-line → no integer push. Integer totals allowed by PDF, not observed live | Half-line → no push in sampled text | Integer totals would be push candidates by convention; **unproven** |

### 4.4 Team total and other labels (evidence only — do not approve)

Observed and **unsafe / deferred** for initial NBA equivalence:

- Kalshi `KXNBATEAMTOTAL` plus period series: `KXNBA1HWINNER`, `KXNBA1HSPREAD`, `KXNBA1HTOTAL`, `KXNBA1Q…` / `2Q` / `3Q` / `4Q` / `2H*`, `KXNBASUMMER*`, player-stat series (`KXNBAPTS`, `KXNBAAST`, …), prepacks, series-win, draft, coach-out, celebrity/all-star.
- Polymarket on Finals G5: `first_half_*`, `basketball_team_to_score_first`, `basketball_odd_even`, player `points` / `rebounds` / `assists` (10+ each).
- Matchbook: NBA **championship outright**; WNBA `1st Half Total` / `1st Half Handicap`; player-prop events if they appear later as separate event ids (NFL census pattern).

---

## 5. Three-provider comparison matrix

| Topic | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| NBA game winner listed now? | Yes — 3 opening-night GAME events | No open games; historical yes | No NBA game; championship outright only |
| Spread / total listed now? | Series exist; **0 open events** | Historical yes; **0 open** | NBA no; WNBA analogue yes |
| Two-way winner (no Draw)? | Yes | Yes | WNBA analogue yes; NBA game unobserved |
| OT in full-game winner? | PDF yes (default entire game = regulation+OT). Market text silent | Market text **yes**. FAQ yes | Unproven |
| OT in spread/total? | PDFs yes unless period specified | FAQ yes. Spread/total **payload text silent** | Unproven |
| Integer lines? | Allowed in PDFs; not sampled live | Not on sampled NBA G5 | WNBA analogue handicap yes |
| Cancel | Fair price (GAME market + PDF 48h) | Market text **50-50**; FAQ **LFMP** — conflict | Unproven |
| Postpone | GAME: remain open if starts within 48h else fair price. TOTALS PDF: delay ≤ two weeks remain open | Market text: remain open until completed. FAQ: until contract expiration (~two weeks) else LFMP | Unproven |
| Suspend / abandon | GAME PDF: 48 minutes NBA play **or** league official result, else fair price. TOTALS PDF uses **24h** not-recommence | FAQ: governing-body official-result threshold, else LFMP | Unproven |
| Venue change | Not named in sampled GAME market text | FAQ: venue change does not void | Unproven |
| Home/away structure | Milestone UUIDs + “Away at Home” title | `teams[].ordering` + sport `ordering=away` | WNBA “X at Y”; NBA game unobserved |
| Exact refresh IDs | market `ticker` (+ event ticker, milestone id) | market `id` + both CLOB tokens (+ event id) | event id + market id (+ runner ids) |
| Graded result fields | Not on archived GAME markets (404) | `umaResolutionStatus=resolved`, `period=VFT`, `score` | NBA game unobserved |

---

## 6. Exceptional lifecycle (rules vs instances)

No postponed, cancelled, suspended, or overtime **instance** was in the live snapshot. Rows below are from current contract text / FAQs, not from a captured exceptional event.

| Case | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Overtime | GAME/SPREAD/TOTAL PDFs: default `<time period>` = regulation + official OT. Quarter/half **exclude** OT unless specified. PDF example: Warriors win 105–104 in OT → full-game Yes. Regulation-only contract would $0.50 if tied at end of regulation. | Moneyline description includes OT. FAQ: Winner, Spread, and Total include OT; NBA has no ties after OT. Spread/total descriptions on G5 omit the word. | Not exposed on NBA game payload. Public Matchbook sports-rules HTML did not return basketball text in this environment (empty/timeout). **Do not import soccer/Betfair/Sky rules.** |
| Tied regulation vs official final | NBA official games continue OT until a winner. PDF still defines $0.50 if the **named period** is tied (Q1, regulation-only). Full-game default should not remain tied. | Moneyline: 50-50 only if canceled or (FAQ) Winner-w/o-tie ends with no winner. Spread text still assigns a **game tie** to the non-cover side. | Unproven. |
| Integer-line push | Exact operator on integer `<count>` can hit equality. Not sampled live. Half-line “over N.5” has no push. | Sampled NBA spreads/totals were half-lines. FAQ says half-points are used to avoid ties. Spread game-tie clause ≠ push. | WNBA analogue offers integer handicaps. Push/void **unproven**. |
| Postponement / reschedule | GAME market + PDF: start within 48h → remain open; else fair price. SPREAD PDF: delay ≤ 48h remain open; >48h fair price. **TOTALS PDF: delay ≤ two weeks remain open; > two weeks fair price.** | Market: remain open until the game has been completed. FAQ: reschedule before expiration (typically two weeks) else LFMP. Home/away flip is a soccer-specific FAQ note, not observed for NBA. | Unproven. |
| Cancellation | GAME: not started within 48h → fair price. PDF: cancel prior to start not rescheduled within 48h → fair price. | Market text: canceled with no make-up → **50-50**. FAQ: canceled and not rescheduled before expiration → **LFMP**. | Unproven. |
| Suspension / abandonment | GAME PDF: incomplete period → fair price; full game settles only if 48 NBA minutes **or** league declares official. SPREAD similar (48h not-recommence). TOTALS PDF: not re-commence within **24 hours** except totals already reached / 48-minute official score. | FAQ: if official-result threshold reached, settle that result; else complete before expiration or LFMP. | Unproven. |
| Venue / date change | Not in sampled market `rules_secondary`. PDF tracks franchise through relocation for `<team>` definition; that is not a same-game venue change rule. | FAQ: venue change has no impact. | Unproven. |
| Void / refund / 50-50 / fair price | Fair price (`MECNET` collateral_return_type on GAME events). Period-tie $0.50 in PDF. | 50-50 in **market descriptions**; LFMP in **US Sports FAQ**. Do not treat those as the same. | Not exposed. |
| Graded result fields | Not recovered for settled GAME (markets archived). | `umaResolutionStatus=resolved`; event `ended`/`score`/`period=VFT`/`finishedTimestamp`. | Championship remaining `open`. WNBA analogue `status=open`. |

---

## 7. Refresh architecture (exact IDs after discovery)

Persist these native IDs; do not rediscover by label.

**Kalshi**

- Persist `series_ticker` + `event_ticker` + market `ticker` (and milestone `id` if tipoff is required).
- Cheap refresh: `GET /events/{event_ticker}` (proven `KXNBAGAME-26OCT20BOSDET`, including `?with_nested_markets=true`); `GET /markets/{ticker}` (proven `KXNBAGAME-26OCT20BOSDET-BOS`); `GET /markets/{ticker}/orderbook` returned `orderbook_fp` for that upcoming ticker.
- Tipoff refresh: milestone `start_date` via `with_milestones=true` on list-events, **not** `occurrence_datetime`.
- `custom_strike.basketball_team` UUID binds the YES side to the milestone home/away UUID.
- Settled GAME markets were **not** exact-GET-able months later. Do not assume graded tickers remain. Sibling SPREAD/TOTAL events were absent for opening night.

**Polymarket**

- Persist Gamma event `id` and market `id`, plus `conditionId` and both `clobTokenIds`.
- Cheap refresh: `GET /events/{id}` (proven `567958`, `209007`, `165960`); `GET /markets/{id}` (proven `2459554`, `2501939`, `2501940`).
- CLOB `GET /book?token_id=` on a **resolved** token returned `No orderbook exists`. Path is still the intended live refresh; it was not proven on an open NBA token because none were listed.
- `gameId` is on the event. Series discovery remains `GET /events?series_id=10345`. Off-season open list was empty; historical discovery used `/public-search`.

**Matchbook**

- Persist numeric `event id`, `market id`, `runner id` (and `event-participant-id` if home/away binding is needed).
- Cheap refresh: `GET /edge/rest/events/{id}`; `GET /edge/rest/events/{id}/markets/{market_id}` (proven for WNBA moneyline and NBA championship outright).
- Filter discovery with `sport-ids=4` and NBA meta-tag `406202315670010`. Do **not** treat WNBA tag `502879947700009` or championship `Outright Winner` as a game event.
- After close, NFL census found full-game markets dropped; NBA game close behaviour is **unproven** because no NBA game event was listed.

---

## 8. Answers to the ten #449 questions

1. **Can all three providers represent the same full-game NBA winner semantics under normal completion?**  
   Kalshi and Polymarket both expose a **two-team winner with no Draw**. Kalshi PDF includes OT; Polymarket moneyline text includes OT. Under a completed official NBA game that is not cancelled/postponed, those two can represent the same “who won the game including OT” proposition **as candidate families**, not as an approved pairing. Matchbook did **not** list an NBA game moneyline in this snapshot; the basketball two-way book was only seen on **WNBA**. Championship outright is a different contract. **Matchbook NBA game-winner pairing is unsupported. Kalshi↔Polymarket is not proven equivalent** (cancel/fair-price vs 50-50/LFMP still differ). Do not reuse soccer GAMEWIN-with-Tie.

2. **How does each provider encode spreads and totals?**  
   - **Kalshi spread:** series `KXNBASPREAD`; PDF is a signed point-differential vs a threshold, OT included by default. No live NBA ladder to show ticker/line encoding.  
   - **Polymarket spread:** `line` signed on the named team; cover = win by `ceil(|line|)`; otherwise the other team, including an explicit game-tie assignment.  
   - **Matchbook spread:** not observed on NBA. WNBA analogue: per-runner signed `handicap` on `market-type=handicap`.  
   - **Kalshi total:** series `KXNBATOTAL`; PDF combined points vs `<count>`, OT included. No live NBA ticks.  
   - **Polymarket total:** Over if combined ≥ ceil(line).  
   - **Matchbook total:** not observed on NBA. WNBA analogue: shared `handicap` OVER/UNDER.

3. **Is overtime included for each exact full-game template?**  
   **Kalshi yes** in GAME/SPREAD/TOTAL PDFs unless a period is specified. Sampled `KXNBAGAME` market `rules_*` **omit** the word overtime — rely on the series PDF, not the short market blurb. **Polymarket moneyline yes** in the market description. **Polymarket spread/total payload unstated**; FAQ says yes. **Matchbook NBA unstated.** Fail closed for PM spread/total OT-from-payload and for all Matchbook NBA OT until an NBA game market carries rule text.

4. **What happens on integer-line pushes?**  
   Polymarket sampled NBA spreads/totals were **half-lines only**, so no integer push instance. Kalshi PDFs allow integer `<count>`; exact equality is a defined operator, not a captured live market. Matchbook WNBA analogue **offers integer handicaps**; push/void is **unproven**. Treat integer lines as **unsafe** for initial equivalence.

5. **What happens on cancellation / postponement / suspension / reschedule?**  
   See §6. Material conflicts: Kalshi GAME 48h vs TOTALS two-week delay vs TOTALS 24h abandon; Polymarket market 50-50 cancel vs FAQ LFMP; Polymarket postpone-until-played vs FAQ expiration window. Matchbook NBA: no payload rules. No live exceptional game.

6. **Are home/away/team labels structurally reliable?**  
   **No.** Use canonical team identity. Kalshi `Los Angeles L` vs `Los Angeles C`, city `New York` vs club `Brooklyn`, title “Away vs Home” plus milestone UUIDs. Polymarket `ordering=away` titles “Away vs Home” with nickname aliases; `lac`/`lal` and `bkn`/`nyk` are structured. Matchbook NBA game labels unobserved. Kickoff clocks: Kalshi `occurrence_datetime` ≠ milestone `start_date`. `eventDate` is ET-ish, not UTC.

7. **What exact native IDs are needed for cheap scheduled repricing?**  
   See §7. Minimum: Kalshi market `ticker`; Polymarket market `id` + both CLOB token ids; Matchbook `event id` + `market id` (+ runner ids for books). Proven GET paths are listed there. Graded Kalshi NBA GAME tickers were **not** refreshable months later.

8. **Which NBA market labels/types are clearly unsafe for initial equivalence?**  
   Period markets (1H/2H/Qn), summer league, team totals, player props, odd/even, first team to score, series/playoff-series and championship outrights, Kalshi prepacks (`KXNBAPREPACK2ML`), coach-out/draft/viewership, WNBA, NCAAB, EuroLeague, soccer 1X2/BTTS/FTTS recognisers, and any integer spread/total until push semantics are proven. Also Kalshi soccer GAME-with-Tie vs NBA GAME-without.

9. **Are x.5 spread and x.5 total the safest initial line families?**  
   **Yes, as candidates**, not as approvals. Polymarket NBA G5 used half-lines only for 41 spreads and 25 totals. Half-lines avoid integer pushes. They still do **not** fix OT wording gaps, Polymarket spread “otherwise/tie” assignment, or Kalshi cancel/fair-price vs Polymarket 50-50/LFMP. Integer Matchbook handicaps (WNBA analogue) are why integers stay deferred.

10. **Are there provider-specific settlement rules that make even normal-completion comparison unsafe?**  
    For a **fully completed, official NBA result with no cancel/postpone**, Kalshi PDF and Polymarket moneyline text both take official final score including OT, two-way, no Draw — that path is the least-unsafe winner comparison, still **not approved**. Remaining normal-path hazards: (a) Kalshi market text vs PDF completeness; (b) Polymarket spread/total descriptions omitting OT while FAQ includes it; (c) Polymarket spread tie/otherwise assignment vs Kalshi differential-threshold binaries; (d) Kalshi GAME vs TOTALS using different delay/abandon clocks, so even same-venue GAME vs TOTAL is not a free copy; (e) Polymarket cancel 50-50 vs FAQ LFMP if anyone treats “normal” as including late official changes; (f) Matchbook NBA game settlement is a complete unknown. Fail closed.

---

## 9. Gaps / ambiguities (must not be guessed in Phase 2)

- Matchbook **NBA game** moneyline/spread/total were **not listed**. Settlement (OT, integer push, postpone, void) has no NBA payload wording.
- Kalshi `KXNBASPREAD` / `KXNBATOTAL` had **no open markets** and settled markets were archived (404). Line ticker encoding is inferred from PDFs + NFL analogy, not from a live NBA ladder.
- Kalshi settled GAME nested markets empty; graded `result` / `expiration_value` **not captured**.
- Polymarket opening-night 2026 games **not listed** yet; no live NBA game; CLOB book not proven on an open NBA token.
- Polymarket spread/total descriptions omit “overtime”; FAQ includes it.
- Polymarket **market 50-50 cancel** vs **FAQ LFMP**.
- Kalshi GAME 48h vs TOTALS 24h/two-week clocks.
- No captured postponed / cancelled / suspended / OT **instance**.
- Unauthenticated Matchbook GETs worked here; production `MatchbookClient` still requires login. This census does not change that client.
- WNBA basketball book shape is **not** NBA evidence for settlement pairing.

---

## 10. What this census does not do

- No NBA production code, alias registry, market equivalence registry, matcher, scanner, cadence, concurrency, settlement, or UI changes.
- No soccer or NFL behaviour change.
- No `owner-live` move.
- No provider writes.
- No claim that NBA families are `APPROVED_EQUIVALENT` or paper-assumed.

Phase 1 is complete when Phase 2 canonical identity rules and Phase 3 approved equivalences can be decided **from this evidence without guessing**. Residual unproven cells are listed in §9 so they stay fail-closed.

---

## 11. Fixture index

All under `backend/tests/fixtures/nba/`. Shape evidence only; prices stripped.

| File | Proves |
|---|---|
| `kalshi_series_kxnbagame.json` (and SPREAD/TOTAL/TEAMTOTAL) | Series tickers + contract-terms URLs |
| `kalshi_event_game_bosdet_open.json` | Upcoming two-way GAME, 48h postpone/cancel fair-price wording |
| `kalshi_event_game_phinyk_open.json` | City `New York` vs ticker `NYK` |
| `kalshi_event_game_okcsas_open.json` | Second upcoming GAME |
| `kalshi_event_game_nyksas_settled.json` | Settled Finals identity; markets archived |
| `kalshi_event_game_gswlac_settled.json` | `Los Angeles C` / `LAC` |
| `kalshi_event_game_nykbkn_settled.json` | `New York` vs `Brooklyn` / `NYK` vs `BKN` |
| `kalshi_milestone_bosdet_tipoff.json` | Tipoff `start_date`, home/away UUIDs |
| `kalshi_market_game_bosdet_bos.json` | Exact `GET /markets/{ticker}` |
| `polymarket_sports_nba.json` | series `10345`, `ordering=away` |
| `polymarket_event_nyk_sas_finals_g5.json` | Ended VFT + ML/spread/total descriptions |
| `polymarket_event_lac_lal_derby.json` | Clippers vs Lakers structured ids |
| `polymarket_event_bkn_nyk_derby.json` | Nets vs Knicks structured ids |
| `polymarket_market_nyksas_moneyline.json` | Exact `GET /markets/{id}` |
| `matchbook_lookups_sports_basketball.json` | sport-id `4` |
| `matchbook_event_nba_championship_2026.json` | Only NBA-tagged event: unsafe outright |
| `matchbook_event_wnba_dream_liberty_shape.json` | Basketball ML/Handicap/Total **shape**, **not NBA** |

Public PDFs (not committed):  
`https://assets.kalshi.com/contract_terms/BASKETBALLGAMEWIN.pdf`, `BASKETBALLSPREADS.pdf`, `BASKETBALLTOTALS.pdf`.  
Public FAQ: `https://docs.polymarket.us/faqs/sports-faqs` (Basketball / overtime / postpone / cancel).

---

## 12. Tenet review

```text
Applicable tenets:
- 02 paper/read-only venue boundary
- 03 canonical market equivalence (fail closed; no NBA pairing implemented)
- 08 provenance of captured payloads
- 11 data honesty (live captured vs unproven; off-season gaps explicit)
- 12 agent review contract
- 20 approved catalogue (NBA not added)

Satisfied:
- 02: GET-only; no execution; no production venue writes
- 03: uncertain pairings marked unsupported
- 08: source URL + retrieved_at on fixtures
- 11: live captured public data, not demo; WNBA shape labelled not NBA
- 20: no silent NBA catalogue approval

Partial / deferred:
- Matchbook NBA game settlement proof (no NBA game book listed)
- Kalshi live/graded SPREAD/TOTAL NBA markets
- Polymarket explicit OT wording on spread/total payloads
- Polymarket cancel 50-50 vs FAQ LFMP
- Exceptional (OT/cancel/postpone) live instances
- Graded Kalshi GAME result fields (archived)

Potential conflicts:
- none. NBA is not wired into soccer/NFL scanners.

Data honesty:
- live captured 2026-09-21 public payloads + public Kalshi PDFs + public Polymarket US Sports FAQ
- committed fixtures are sanitized shapes, not quote streams
- Matchbook WNBA fixture is demo-of-shape only and is labelled not NBA

Safety:
- paper-only boundary preserved; no production behaviour changed
```
