# NCAAB Phase 1 — Provider Evidence Census

**Issue:** #508  
**Base:** exact owner-live `cac3b25814c08707e66d7b9ac41564658a874aa0` (after #501/#503)  
**Mode:** PAPER MODE · EXECUTION DISABLED · GET / read-only only  
**Data class:** LIVE captured public market-data payloads from 2026-09-22 ~14:47 UTC, plus Kalshi public series contract-term URLs. Not modelled probabilities. Not demo/fixture soccer or NFL data. Prices are omitted from committed fixtures.

This is an evidence-gathering census. It does **not** implement NCAAB production matching, alias/equivalence registries, scanner/cadence/concurrency, settlement, or UI. Soccer and NFL behaviour are unchanged. `owner-live` is not moved.

Sanitized shape fixtures: `backend/tests/fixtures/ncaab/`.

---

## 0. Method and safety

1. Branched from exact owner-live `cac3b25`.
2. Used the same public hosts already configured for Phase 1 (`KALSHI_BASE_URL`, `POLYMARKET_GAMMA_BASE_URL`, `MATCHBOOK_BASE_URL`) plus ESPN's public site API for the current Division I men's roster.
3. **GET only.** No Matchbook `POST /bpapi/rest/security/session`. No order, cancel, wallet, or signing calls.
4. Matchbook event/market/sports GETs succeeded **without credentials** in this environment. `MatchbookClient.login()` was not used. That is a capture fact, not a production-client change.
5. Did not guess missing settlement. Incomplete Matchbook NCAAB game rule text and absent NCAAB game books are recorded as unproven.
6. This capture is **off-season** relative to NCAA Division I men's 2026–27 (opening night **2026-11-02**). Kalshi game/spread/total series exist with **zero open events**. Polymarket CBB series `10470` has **zero open games**. Matchbook basketball currently lists WNBA games and an NBA championship outright — **no NCAAB competition tag**.

Applicable tenets: 02 (paper/read-only), 03 (do not fuzzy-match settlement), 08 (provenance), 11 (data honesty), 12 (agent review), 20 (catalogue — NCAAB families are **not** PAPER-approved here).

---

## 1. Capture window

| Field | Value |
|---|---|
| Retrieved | 2026-09-22 ~14:47 UTC (Tuesday; NCAAB 2026–27 not started) |
| Upcoming Kalshi GAME/SPREAD/TOTAL | **none open** |
| Kalshi active CBB outrights | Men's 2027 Tournament Champion (`KXMARMAD-27`, 73 markets); round-of-16 / Final Four qualifiers; regular-season win totals; AP poll; conference regular-season champions; undefeated regular season |
| Polymarket series `10470` (`cbb`) open games | **none** (`closed=false` empty; historical 2025-11-03 games exist) |
| Polymarket `ncaab` series `39` | March Madness route — **not** the fixture game book |
| Polymarket `cwbb` series `10471` | Women's College Basketball — **out of scope** |
| Matchbook basketball sport-id 4 | 6 events: 5 WNBA games + NBA Championship Winner 2026/27. **Zero NCAAB games.** No NCAA/NCAAB/college-basketball tag. |
| Live NCAAB games | **none** in this snapshot |

---

## 2. Discovery IDs actually used

| Provider | Discovery | Native IDs |
|---|---|---|
| Kalshi | `GET /series/{ticker}`; `GET /events?series_ticker=&status=open\|settled&with_nested_markets=true&with_milestones=true` | Men's game series `KXNCAAMBGAME`, `KXNCAAMBSPREAD`, `KXNCAAMBTOTAL`. Legacy empty `KXNCAABGAME`. Women's `KXNCAAWBGAME` (exclude). Championship outright `KXMARMAD`. Event ticker `KXNCAAMBGAME-26APR04MICHARIZ`. Milestone `type=basketball_game`, `details.league=NCAAMB`. |
| Polymarket | `GET /sports` → College Basketball `sport=cbb`, `series=10470`, `ordering=away`; `GET /events?series_id=10470` | Historical event id `68009`, slug `cbb-morgst-george-2025-11-03`, market ids + `conditionId` + `clobTokenIds`, `gameId` `68285`. March Madness `sport=ncaab` / series `39` is a **different** route. |
| Matchbook | `GET /edge/rest/lookups/sports` → Basketball `id=4`; `GET /edge/rest/events?sport-ids=4`; `tag-url-names=ncaa\|ncaab` | Basketball sport-id `4`. Observed competition tags: WNBA `502879947700009`, NBA `406202315670010`. **No NCAAB competition tag.** `tag-url-names=ncaa` and `ncaab` returned zero events. |
| ESPN (roster only) | `GET /apis/site/v2/sports/basketball/mens-college-basketball/teams?limit=500` | 362 active NCAA Men's Basketball programs. Identity source for the Division I registry. Not a betting venue. |

`status=closed` on Kalshi `/events` returned empty for GAME. Graded games were under `status=settled` **as event shells** (nested markets empty). Guessed settled market tickers (`…-MICH`, `…-ARIZ`) returned HTTP 404.

---

## 3. Event identity

### 3.1 Same game — not currently listed on all three venues

No live three-venue game exists in this window. Identity rules are taken from historical/settled shells plus the NFL/NBA census pattern.

| Field | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Native event id | `KXNCAAMBGAME-26APR04MICHARIZ` (settled shell) | Historical `68009` / `cbb-morgst-george-2025-11-03` | **not listed** |
| Title | `Michigan at Arizona` | `Morgan State Bears vs. Georgetown Hoyas` | — |
| Subtitle / labels | `MICH at ARIZ (Apr 4)` | teams[] `Morgan State Bears` / `Georgetown Hoyas`, abbr `morgst` / `george` | — |
| Home / away | Milestone title `Michigan at Arizona`; `home_team_id` Arizona UUID, `away_team_id` Michigan UUID; event title is **away at home** | `teams[].ordering`: Morgan State `away`, Georgetown `home`; sport `ordering=away` | — |
| Tipoff | Milestone `start_date` `2026-04-05T01:19:00Z`. Event object has **no** `occurrence_datetime` in this settled shell | `startTime` `2025-11-03T23:30:00Z`. `eventDate` is `2025-11-03` (ET calendar date) | — |
| League | `product_metadata.competition=CBB Tournament`; milestone `details.league=NCAAMB`; `special_event_type=march_madness` | sport object `cbb` / series `10470` | No NCAAB tag |
| Status | Event object has **no** `status` field. Nested markets empty | `ended=true`, `closed=true`, `period=FT`, `score=70-87` | — |

**Do not use Kalshi expiration / missing occurrence fields as scheduled tipoff.** Use milestone `start_date`. Same pattern as NFL/NBA Phase 1.

Nearby tournament games must never collapse merely because tipoff and abbreviated school names are similar. Milestone `special_event_type=march_madness` is supporting context, not a join key.

### 3.2 Ambiguous school labels (must remain distinct)

Canonical team identity **must** drive ordering. Evidence and required failures:

| Collision | Must remain distinct |
|---|---|
| Miami (FL) vs Miami (OH) | ESPN `Miami Hurricanes` / abbr `MIA` vs `Miami (OH) RedHawks` / `M-OH`. Generic `Miami` is **ambiguous**. |
| USC vs South Carolina | ESPN `USC Trojans` / `USC` vs `South Carolina Gamecocks` / `SC`. Generic `Carolina` / `SC` / `Southern` are not globally unique. |
| St. John's vs Saint Joseph's vs Saint Mary's | `SJU` / `JOES` / `SMC`. Generic `Saint` / `St` / `St. John's` lookalikes without school context fail closed. |
| Loyola Chicago vs Loyola Marymount vs Loyola Maryland | `LUC` / `LMU` / `L-MD`. Generic `Loyola` is **ambiguous**. |
| Multiple State / Tech / A&M schools | Generic `State`, `Tech`, `A&M` are **ambiguous**. |
| Abbreviations that are not globally unique | ESPN abbreviations in this snapshot were unique; provider short forms such as `UT` / `Texas` / `Tennessee` still require curated disambiguation. |

Conference can be supporting identity evidence. It must **not** be treated as immutable team identity.

---

## 4. Market identity

### 4.1 Game winner / moneyline (full game) — structural candidate only

Observed basketball game winners are **two-team**, not soccer 1X2.

| | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Family | `KXNCAAMBGAME`; `product_metadata.competition_scope=Game` | Historical `sportsMarketType=moneyline` | **NCAAB game Moneyline not listed.** WNBA analogue: `name=Moneyline`, `market-type=money_line` |
| Market id | Settled markets **archived** (GET guessed ticker → 404) | Gamma `655763`; CLOB token ids present on historical book | WNBA analogue market ids exist; **not NCAAB** |
| Sides | Settled shell `mutually_exclusive=true`. Nested YES contracts **not recovered** | Outcomes `["Morgan State Bears","Georgetown Hoyas"]`. **No Draw.** | WNBA analogue: two club-named runners, **no Draw** |
| Period | Series contract-terms URL is **`ACHIEVEMENTS.pdf`** (generic). **Not** a basketball GAMEWIN PDF in this capture | Description: “final score including any overtime periods”; cancel → 50-50; postpone remains open | No NCAAB game rule text |
| Parent/child | GAME vs SPREAD/TOTAL are **different event tickers** when listed | Many child markets on one Gamma event when listed | Markets listed under one event id |

**PAPER admission blocked.** Kalshi NCAAB GAME settlement wording was not recovered from nested markets, and the series PDF is a generic achievements template rather than a proven NCAAB game-winner contract.

### 4.2 Point spread — structural candidate (exact x.5 only)

| | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Shape | Series `KXNCAAMBSPREAD` exists. **Zero open events.** Settled shells have no markets. Contract PDF `BASKETBALLSPREADS.pdf` | Historical: `Spread: Georgetown Hoyas (-24.5)`, signed `line=-24.5`, outcomes `[Georgetown Hoyas, Morgan State Bears]` | NCAAB Handicap **not listed**. WNBA analogue: `Handicap` with integer and half lines |
| Integer vs half | PDF allows whole or half. **No live NCAAB ladder sampled** | Sampled historical CBB spreads in this snapshot included half-lines | WNBA analogue offers integer handicaps — **not NCAAB proof** |

**PAPER admission blocked.** No live Kalshi NCAAB spread ladder; Polymarket spread description overtime/push handling is not independently proven against Kalshi NCAAB; Matchbook has no NCAAB book.

### 4.3 Game total points — structural candidate (exact x.5 only)

| | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Shape | Series `KXNCAAMBTOTAL`. **Zero open events.** PDF `BASKETBALLTOTALS.pdf` | Historical: O/U with Over/Under outcomes and a numeric `line` | NCAAB Total **not listed**. WNBA analogue: `Total` Over/Under |
| OT | PDF: regulation and overtime unless period specified | Historical moneyline text includes OT; sampled total description must not be assumed identical without the payload | Unproven for NCAAB |

**PAPER admission blocked.**

### 4.4 Explicitly rejected / deferred (not FIXTURE_MATCH)

- Kalshi women's `KXNCAAWB*` / Polymarket `cwbb` / NCAAW
- Kalshi `KXNCAABGAME` (legacy “College Basketball Game”, **zero events**)
- Conference / championship / March Madness outrights (`KXMARMAD*`, `KXNCAAMBACC`, Big Ten/SEC/etc. tournament series)
- Win totals, AP poll, undefeated, NIT, seeds, bracket advancement
- Halves / quarters / first-to-N / same-game parlays / D3 (`KXNCAAMBD3GAME`)
- Team totals (no `KXNCAAMBTEAMTOTAL` series — 404)
- Integer spread/total lines
- Player props
- NBA / WNBA / G League / Summer League
- Polymarket `ncaab` series `39` (March Madness page, not the CBB game series)

---

## 5. Three-provider comparison matrix

| Topic | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| NCAAB game winner listed now? | Series exists; **0 open events** | Series `10470` exists; **0 open games**; historical yes | **No NCAAB game**; WNBA + NBA championship only |
| Spread / total listed now? | Series exist; **0 open events** | Historical yes; **0 open** | NCAAB no; WNBA analogue yes |
| Two-way winner (no Draw)? | Settled shell mutually exclusive; markets archived | Historical yes | WNBA analogue yes; NCAAB unobserved |
| OT in full-game winner? | Series PDF is ACHIEVEMENTS.pdf — **not proven** | Historical moneyline text **yes** | Unproven |
| Cancel | `collateral_return_type=MECNET` (fair-price family) on settled shell | Market text **50-50** | Unproven |
| Postpone | Not recovered on GAME nested rules | Remain open until completed | Unproven |
| Home/away structure | Milestone UUIDs + “Away at Home” title | `teams[].ordering` + sport `ordering=away` | Unobserved for NCAAB |
| Exact refresh IDs | market `ticker` (+ event ticker, milestone id) — **graded tickers not GET-able months later** | market `id` + both CLOB tokens (+ event id) | event id + market id (+ runner ids) when a book exists |
| Graded result fields | Not on archived GAME markets (404) | `umaResolutionStatus=resolved`, `period=FT`, `score` | NCAAB game unobserved |

---

## 6. Exceptional lifecycle (rules vs instances)

No postponed, cancelled, suspended, or overtime **instance** was in the live snapshot. Rows below are from current contract text / historical market descriptions, not from a captured exceptional NCAAB event.

| Case | Kalshi | Polymarket | Matchbook |
|---|---|---|---|
| Overtime | GAME series PDF not basketball-specific in this capture. SPREAD/TOTAL PDFs are basketball templates | Historical CBB moneyline includes OT | Not exposed on an NCAAB payload |
| Cancellation | Fair-price / MECNET on settled GAME shell | CBB moneyline: canceled with no make-up → **50-50** | Unproven |
| Postponement | Not recovered on nested GAME markets | Remain open until completed | Unproven |
| Void / 50-50 / fair price | Fair price indicated by collateral return type | 50-50 in **market descriptions** | Not exposed |
| Graded result fields | Markets archived (404) | `umaResolutionStatus=resolved` | NCAAB unobserved |

Kalshi fair-price vs Polymarket 50-50 is an **exceptional cross-venue mismatch**. Automatic settlement must not treat a Kalshi-only finalized ticker as proof that a Polymarket (or Matchbook) leg completed normally.

---

## 7. Refresh architecture (exact IDs after discovery)

Persist these native IDs; do not rediscover by label.

**Kalshi**

- Persist `series_ticker` + `event_ticker` + market `ticker` (and milestone `id` if tipoff is required).
- Tipoff refresh: milestone `start_date` via `with_milestones=true`, **not** expiration.
- Settled GAME markets were **not** exact-GET-able months later. Do not assume graded tickers remain.
- When NCAAB is in operator scope, query only `KXNCAAMBGAME` / `KXNCAAMBSPREAD` / `KXNCAAMBTOTAL`. Do not query the entire Sports series list. Zero open events is healthy.

**Polymarket**

- Persist Gamma event `id` and market `id`, plus `conditionId` and both `clobTokenIds`.
- Series discovery remains `GET /events?series_id=10470`. Off-season open list was empty; historical discovery used `closed=true`.
- Do **not** treat Gamma sport `ncaab` / series `39` (March Madness) as the fixture game book.
- Missing CLOB tokens, or invented `condition_id:0/1` placeholders, fail closed.

**Matchbook**

- No NCAAB competition tag was observed. Discovery with basketball sport-id `4` currently returns WNBA + NBA championship and must **reject** those after the collector-boundary gate.
- `GET /edge/rest/events?tag-url-names=ncaa` and `ncaab` returned zero. Do not invent a tag id.
- When an NCAAB COMPETITION tag appears in a later census, narrow discovery to that tag. Until then, NCAAB-selected Matchbook discovery may use sport-id `4` inside the existing concurrency slot and must count rejected non-NCAAB basketball honestly.
- Persist numeric `event id`, `market id`, `runner id` if/when an NCAAB game book is listed.

---

## 8. PAPER venue-pair / family matrix (this census)

No venue-pair/family cell is PAPER-admitted. Structural normalisation of GAME_WINNER + exact x.5 SPREAD/TOTAL may proceed for diagnostics/catalogue identity. Equivalence and automatic settlement stay fail-closed.

| Family | Matchbook ↔ Kalshi | Matchbook ↔ Polymarket | Kalshi ↔ Polymarket |
|---|---|---|---|
| GAME_WINNER | **blocked** — no Matchbook NCAAB game book; Kalshi GAME markets archived; series PDF not NCAAB-specific | **blocked** — no Matchbook NCAAB game book | **blocked** — no live books; Kalshi nested GAME rules not recovered; cancel path fair-price vs 50-50 |
| POINT_SPREAD x.5 | **blocked** | **blocked** | **blocked** |
| TOTAL_POINTS x.5 | **blocked** | **blocked** | **blocked** |

---

## 9. Current Division I team registry source

| Field | Value |
|---|---|
| Source | ESPN public site API `mens-college-basketball/teams?limit=500` |
| Retrieved | 2026-09-22T14:47:25Z |
| League label | NCAA Men's Basketball |
| Active programs | **362** |
| Fixture | `backend/tests/fixtures/ncaab/espn_d1_mens_team_roster.json` |

This is the identity evidence set for the deterministic Division I men's registry. It is not a betting venue and is not live odds.

---

## 10. Fixture index

All under `backend/tests/fixtures/ncaab/`. Shape evidence only; prices stripped.

| File | Proves |
|---|---|
| `kalshi_series_kxncaambgame.json` (and SPREAD/TOTAL) | Men's game/spread/total series tickers + contract-terms URLs |
| `kalshi_series_kxncaabgame.json` | Legacy empty College Basketball Game series |
| `kalshi_series_kxncaawbgame.json` | Women's series exists and must be excluded |
| `kalshi_events_game_open_empty.json` | Healthy zero open GAME events |
| `kalshi_event_game_michariz_settled_shell.json` | Settled identity + archived markets |
| `kalshi_milestone_michariz_tipoff.json` | Tipoff `start_date`, home/away UUIDs, league `NCAAMB` |
| `kalshi_event_marmad_champion_outright.json` | Active futures; out of FIXTURE_MATCH |
| `polymarket_sports_cbb.json` | series `10470`, `ordering=away` |
| `polymarket_sports_ncaab.json` | March Madness series `39` — not the game book |
| `polymarket_sports_cwbb.json` | Women's series `10471` — out of scope |
| `polymarket_events_cbb_open_empty.json` | Healthy zero current CBB games |
| `polymarket_event_cbb_del_buck_moneyline.json` | Historical moneyline-only shape |
| `polymarket_event_cbb_morgst_george_families.json` | Historical ML + spread + total + OT wording |
| `matchbook_lookups_sports_basketball.json` | sport-id `4` |
| `matchbook_events_basketball_current.json` | Current basketball slate; no NCAAB tag |
| `matchbook_event_wnba_analogue_not_ncaab.json` | WNBA analogue is not NCAAB proof |
| `espn_d1_mens_team_roster.json` | 362-team Division I identity source |

---

## 11. Tenet review

```text
Applicable tenets:
- 02 paper/read-only venue boundary
- 03 canonical market equivalence (fail closed; no NCAAB pairing implemented)
- 08 provenance of captured payloads
- 11 data honesty (live captured vs unproven)
- 12 agent review contract
- 20 approved catalogue (NCAAB not PAPER-admitted)

Satisfied:
- 02: GET-only; no execution; no production venue writes
- 03: uncertain pairings marked unsupported / blocked
- 08: source URL + retrieved_at on fixtures
- 11: live captured public data, not demo
- 20: no silent NCAAB catalogue approval

Partial / deferred:
- Matchbook NCAAB competition tag / game book
- Kalshi nested GAME market rules and graded tickers
- Live 2026–27 fixture books (season opens 2026-11-02)

Potential conflicts:
- none. NCAAB is not wired into soccer scanners in this census commit.

Data honesty:
- live captured 2026-09-22 public payloads
- committed fixtures are sanitized shapes, not quote streams
- ESPN roster is identity evidence, not live odds

Safety:
- paper-only boundary preserved; no production behaviour changed in this census commit
```
