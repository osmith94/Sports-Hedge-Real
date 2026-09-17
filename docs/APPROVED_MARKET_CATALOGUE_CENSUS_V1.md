# Approved Market Catalogue Census v1

**Issue:** #268  
**Parent:** R4 `620d1807473a5bfaa08a5023d2a28f4da756efe3`  
**Tenet:** Core Tenet 20 — Approved Market Catalogue and Exception Review  
**Mode:** PAPER MODE · EXECUTION DISABLED  
**Data class:** DETERMINISTIC FIXTURE / DEMO, plus cited captured public payloads and an optional read-only public Kalshi/Polymarket spot-check. Not owner-live Matchbook. Not historical quotes. Not modelled probabilities.

This census verifies the intended Phase-1 football catalogue across Matchbook, Kalshi and Polymarket. Approval is **pairwise**. A generic confidence score is never executable permission. Incomplete source evidence is `REVIEW_REQUIRED`. A family that exists in code but cannot be modelled by the solver is not operationally approved. Support is never invented.

HOT and UNIVERSE share this catalogue. The classifier and admission gate have no `scan_lane`. Matcher recognition semantics, HOT/UNIVERSE concurrency, paper autofill and execution are unchanged.

Production solver/paper admission requires `APPROVED_EQUIVALENT`. `REVIEW_REQUIRED`, `UNSUPPORTED`, `APPROVED_PARAMETER_MISMATCH` and `KNOWN_CONTRADICTION` cannot reach the solver or paper admission.

Executable matrix source: `backend/src/sports_hedge/catalogue/matrix.py`  
Corpus: `backend/src/sports_hedge/catalogue/corpus.py`  
Classifier: `backend/src/sports_hedge/catalogue/classify.py`  
Admission gate: `backend/src/sports_hedge/catalogue/admission.py`

---

## 1. Pairwise approval matrix

Cells are the operational verdict **when required settlement evidence is present**. Sibling `REVIEW_REQUIRED` / `UNSUPPORTED` cases are listed under each archetype and locked by the fixture corpus.

| Archetype | Matchbook↔Kalshi | Matchbook↔Polymarket | Kalshi↔Polymarket |
|---|---|---|---|
| Match Result / 1X2 | APPROVED_EQUIVALENT | APPROVED_EQUIVALENT | APPROVED_EQUIVALENT |
| Both Teams To Score | APPROVED_EQUIVALENT | APPROVED_EQUIVALENT | APPROVED_EQUIVALENT |
| Total Goals O/U (half-line) | APPROVED_EQUIVALENT | APPROVED_EQUIVALENT | APPROVED_EQUIVALENT |
| Total Goals O/U (integer) | UNSUPPORTED | APPROVED_EQUIVALENT | UNSUPPORTED |
| First Team To Score | APPROVED_EQUIVALENT | APPROVED_EQUIVALENT | APPROVED_EQUIVALENT |
| Team Total Goals O/U | UNSUPPORTED | REVIEW_REQUIRED | UNSUPPORTED |
| Handicap | UNSUPPORTED | UNSUPPORTED | UNSUPPORTED |
| Draw No Bet | UNSUPPORTED | APPROVED_EQUIVALENT | UNSUPPORTED |
| Double Chance | UNSUPPORTED | UNSUPPORTED | UNSUPPORTED |
| Team To Score | UNSUPPORTED | UNSUPPORTED | UNSUPPORTED |
| Team Clean Sheet | UNSUPPORTED | UNSUPPORTED | UNSUPPORTED |

`APPROVED_EQUIVALENT` here means: both venues can recognize the archetype, required parameters agree, settlement fingerprints are economically complete and identical, the outcome state space is complete, and Sports Hedge can model every settlement state (simple complete-set or generalized payoff).

---

## 2. Method

1. Read existing venue recognisers, settlement fingerprint construction, matcher gates and solver eligibility at the R4 parent.
2. Do not auto-approve from market labels, series tickers (`KXEPLGAME` / `BTTS` / `TOTAL` / `FTTS`) or confidence.
3. Classify each pair into Tenet 20 states. Incomplete settlement is `REVIEW_REQUIRED` even when `MarketMatcher` currently matches Matchbook↔Kalshi GAMEWIN-unknown ordinary 1X2.
4. Gate production `scan_eligible_pair` and `PaperScanService.scan_pair` on `APPROVED_EQUIVALENT`. HOT and UNIVERSE call the same function.
5. Lock known-good and known-bad fixture payloads in a versioned corpus.
6. Use captured public payloads already in the repository where available. Do not request or expose Matchbook credentials.
7. Report family-level before (matcher + solver-model capability) vs after (catalogue state / production admission).

Matchbook full-time football markets still use the documented venue convention that payloads carry no resolution-rule text and full-time books are treated as regulation / no extra-time / no penalties (`_standard_football_settlement`). That is a version-controlled convention, not a label guess. Polymarket and Kalshi require parsed rule wording.

---

## 3. Recognition / settlement / solver evidence

### 3.1 Match Result / 1X2

Required contract: `HOME` / `DRAW` / `AWAY`, full time, regulation, extra-time false, penalties false, no push. Solver: simple complete-set.

**Matchbook**

- Recognition: exact name in `{match odds, match result, moneyline, full time result}`. Runners must be named home/away teams plus Draw. Generic `Home`/`Away` map to `OTHER` and do not complete 1X2.
- Settlement: documented full-time convention; no venue rule text.
- Solver: complete H/D/A + complete fingerprint → simple complete-set.

**Kalshi**

- Recognition: YES contracts whose `yes_sub_title` maps to HOME/DRAW/AWAY (or title contains match result / moneyline / to win after other families are rejected). Series ticker is not read. Assembly requires exhaustive H/D/A sharing one fingerprint.
- Settlement: `rules_primary` / `rules_secondary` / `rules` via fail-closed wording. Event-level rules inherit only for Match Result. Get Market enrichment exists **for Match Result only**. `SOCCERGAMEWIN` has no default `<result scope>`; placeholder unavailability is recorded as `soccergamewin_result_scope_placeholder_unavailable`.
- Solver capability: complete regulation 1X2 → simple complete-set. GAMEWIN-unknown still has a simple-solver *capability* via the documented Matchbook↔Kalshi ordinary-1X2 narrowing. **Catalogue state is `REVIEW_REQUIRED`. Production admission now blocks it.**

**Polymarket**

- Recognition: `sportsMarketType` contains `moneyline`, or question contains `match result` / `to win`. A lone Yes/No moneyline stays binary and is not a 3-way 1X2. Complementary HOME+DRAW+AWAY Yes contracts can be promoted when fingerprints agree.
- Settlement: `description` / `rules` / `resolutionCriteria` via `classify_settlement_wording`. Missing or unparsed wording is UNKNOWN. Unparsed extra-time after a 90-minute marker stays UNKNOWN (Issue #210 fail-closed).
- Solver: complete 3-way regulation → simple complete-set. Unknown Polymarket 1X2 does **not** receive the GAMEWIN exception.

### 3.2 Both Teams To Score

Required contract: `YES` / `NO`, regulation, no push. Solver: simple complete-set.

**Matchbook:** name contains `both teams to score` or equals `btts`. Full-time convention.

**Kalshi:** title/rules contain `both teams to score` or `btts`. Exactly one binary contract assembles to YES+synthetic NO. Get Market is **not** fetched. `socceranygoal` extra-time default is **not** applied. `"See contract URL."` is family-recognized with UNKNOWN fingerprint → `REVIEW_REQUIRED`.

**Polymarket:** `sportsMarketType` / question contain BTTS. Description must parse to regulation for approval.

### 3.3 Total Goals Over/Under

Parameterized: exact line must match. Half-line (2.5): no push, simple complete-set. Integer (2.0): push modelled, generalized payoff. Quarter/split lines: push unknown → `REVIEW_REQUIRED`, not approved.

**Matchbook:** `"total goal"` or (`over under` and `goal`). Participant-id, one named team, or `team total` tokens fail closed to `TEAM_TOTAL`.

**Kalshi:** match totals only. Named-team totals raise (not inferred as match totals). Missing line raises. `line_push_possible is not False` raises: integer/quarter remain deferred. Get Market not fetched. `SOCCERTOTAL.pdf` / `socceranygoal` extra-time default is not applied.

**Polymarket:** match totals vs team-scoped titles (`Total Goals Over 2.5 - Arsenal` is `TEAM_TOTAL`). Integer 2.0 is generalized-eligible when wording is complete.

### 3.4 First Team To Score

Required operational model: `HOME` / `AWAY` / `NO_GOAL`, full time, regulation, extra-time false, penalties false, push false. Solver: generalized payoff (`home_first` / `away_first` / `no_goal`). Two-team books without NO_GOAL are not equivalent (Tenet 20 §9).

**Matchbook:** explicit FTTS phrases, or ambiguous `first goal` only when runners are team-level H/A/NO_GOAL. Missing NO_GOAL still normalizes as FTTS but is incomplete.

**Kalshi:** requires HOME+AWAY+NO_GOAL **and** proven `REGULATION_TIME` or assembly raises. Incomplete FTTS never becomes a CanonicalMarket. Get Market is **not** fetched for FTTS, so nested generic wording cannot be completed today.

**Polymarket:** explicit FTTS or 3 team-level outcomes. Regulation description matches Matchbook convention. Extra-time description is a known contradiction. Player first-goalscorer is `PLAYER_PROPS`, not FTTS.

### 3.5 Team Total Goals Over/Under

Required contract if operational: exact named team + exact line + Over/Under. **Not operational in v1.**

**Matchbook / Polymarket:** can recognize `TEAM_TOTAL` (named-team totals wording / `groupItemTitle`). `CanonicalMarket` has no team-scope field, so the required parameter is not extracted. Solver cannot model team totals. MB↔PM is `REVIEW_REQUIRED` (`team_scope_not_extracted_on_canonical_market`).

**Kalshi:** named-team totals are not inferred. MB↔K and K↔PM are `UNSUPPORTED`.

### 3.6 Handicap

Required contract if operational: exact team, exact handicap type, exact line. **Not operational in v1.**

**Matchbook / Polymarket:** Asian Handicap / handicap can be recognized. Semantics remain unproven and solver-ineligible → `UNSUPPORTED` (`unproven_handicap_semantics`).

**Kalshi:** Asian Handicap is not inferred from titles → `UNSUPPORTED`.

### 3.7 Draw No Bet

Required contract: HOME/AWAY with proven draw-void (`push_possible True`) and complete regulation fingerprints. Solver: generalized payoff.

**Matchbook / Polymarket:** recognized as `DRAW_NO_BET`. Family push convention is draw-void. Complete regulation wording on Polymarket plus Matchbook full-time convention is `APPROVED_EQUIVALENT` on the generalized path.

**Kalshi:** Draw No Bet remains deferred until draw-refund rules are proven. Assembly raises. MB↔K and K↔PM are `UNSUPPORTED`.

### 3.8 Double Chance

Required contract if operational: exact 1X / 12 / X2 combination. **Not operational in v1.**

**Matchbook:** name `Double Chance` is recognized. No solver model.

**Polymarket:** no Double Chance recogniser (`Unsupported Polymarket sports market`). Do not invent one here.

All three venue pairs are `UNSUPPORTED`.

### 3.9 Team To Score

Not First Team To Score. Exact named team + Yes/No settlement.

No distinct recogniser on Matchbook, Kalshi or Polymarket. Titles such as `Tottenham To Score` / `Will Tottenham score?` fail closed as unsupported venue markets. All pairs `UNSUPPORTED`.

### 3.10 Team Clean Sheet

Exact named team + Yes/No settlement.

No recogniser on any v1 venue. Titles such as `Tottenham Clean Sheet` fail closed. All pairs `UNSUPPORTED`.

---

## 4. Explicit REVIEW_REQUIRED examples

| Corpus id | Pair | Why |
|---|---|---|
| `bad-1x2-mb-k-gamewin-unknown` | MB↔K 1X2 | Kalshi GAMEWIN listed scopes; no recoverable `<result scope>`. Catalogue: incomplete settlement. Matcher still matches. Production gate blocks solver/paper. |
| `bad-1x2-mb-pm-unknown-settlement` | MB↔PM 1X2 | Polymarket wording does not parse to a complete fingerprint. |
| `bad-btts-k-ambiguous-rules` | MB↔K BTTS | Kalshi `"See contract URL."` is recognized as BTTS but settlement is UNKNOWN. No Get Market enrichment for BTTS. |
| `bad-ftts-missing-no-goal-both` | MB↔PM FTTS | Both books are HOME/AWAY only. 0-0 / void contract is unproven. Solver cannot model the missing state. |
| `bad-ftts-k-unproven-regulation` | MB↔K FTTS | Kalshi FTTS with `"Winner of the match."` does not assemble. |
| `review-team-total-mb-pm` | MB↔PM team totals | Family recognized; named-team parameter is not on `CanonicalMarket`. |

Also REVIEW_REQUIRED (not all in corpus): lone Polymarket moneyline binary vs 3-way 1X2; Kalshi totals with incomplete nested rules; quarter-line totals (`line_push_possible is None`).

**High confidence does not approve these.** A 0.99 market confidence with UNKNOWN settlement is still `REVIEW_REQUIRED`.

---

## 5. Explicit unsupported / deferred list

**Unsupported for v1 operational admission:**

- Player first-goalscorer / anytime scorer (`PLAYER_PROPS`)
- Next Goal
- To Qualify
- Kalshi Draw No Bet (deferred until draw-refund proven)
- Double Chance (no solver; Polymarket has no recogniser)
- Asian Handicap (recognized on MB/PM, unproven, no solver)
- Team To Score, Team Clean Sheet (no recogniser)
- Corners, cards, correct score, HT/FT, player props
- Matchbook names outside the recogniser (`"Total"`, `"1st Half Total"` without a goals phrase)
- Kalshi titles that do not carry a family phrase (series ticker is not a recogniser)
- Kalshi integer/quarter Total Goals and Kalshi team totals (not assembled)

**Deferred until rules or model are proven:**

- Team-scope extraction on `CanonicalMarket` (blocks team-total approval)
- Kalshi integer/quarter Total Goals
- Kalshi Draw No Bet
- Split/quarter totals on Matchbook/Polymarket (push unknown → `REVIEW_REQUIRED`, not approved)
- Applying `socceranygoal` / `SOCCERTOTAL` extra-time defaults to BTTS or totals (not applied; live selected-clause evidence still required)
- Double Chance / Handicap solver models

---

## 6. Known-good / known-bad corpus

Executable corpus: `sports_hedge.catalogue.corpus.census_corpus()` (35 deterministic fixture entries).

**Known-good (`APPROVED_EQUIVALENT`):**

- 1X2 MB↔PM, MB↔K complete regulation, K↔PM complete regulation
- BTTS MB↔PM, MB↔K complete, K↔PM complete
- Totals 2.5 MB↔PM, MB↔K, K↔PM
- Totals 2.0 MB↔PM (generalized push)
- FTTS 3-state regulation MB↔PM, MB↔K, K↔PM
- Draw No Bet MB↔PM (generalized draw-void)

**Known-bad:**

- GAMEWIN-unknown 1X2 → `REVIEW_REQUIRED` (matcher may still match; production gate blocks)
- Polymarket unknown 1X2 → `REVIEW_REQUIRED`
- First-half vs full-time 1X2 → `APPROVED_PARAMETER_MISMATCH`
- Extra-time vs regulation 1X2 / FTTS → `KNOWN_CONTRADICTION`
- BTTS Kalshi ambiguous rules → `REVIEW_REQUIRED`
- Totals 2.5 vs 3.5 → `APPROVED_PARAMETER_MISMATCH`
- Integer totals MB↔K → `UNSUPPORTED`
- Team total vs match total → `KNOWN_CONTRADICTION`
- Team totals MB↔PM → `REVIEW_REQUIRED`; MB↔K → `UNSUPPORTED`
- Handicap MB↔PM / MB↔K → `UNSUPPORTED`
- Draw No Bet MB↔K → `UNSUPPORTED`
- Double Chance MB↔PM → `UNSUPPORTED`
- Team To Score / Clean Sheet MB↔PM → `UNSUPPORTED`
- FTTS missing NO_GOAL both sides → `REVIEW_REQUIRED`
- FTTS asymmetric NO_GOAL → `KNOWN_CONTRADICTION`
- Kalshi FTTS without regulation → `REVIEW_REQUIRED`
- Player first-goalscorer vs FTTS → `UNSUPPORTED`

---

## 7. Family-level before / after coverage

Before = R4 `MarketMatcher.matched` **and** `solver_model_for_pair` capability on the same fixture corpus.  
After = Tenet 20 catalogue state. Production admission uses After.  
This is not owner-live coverage.

Corpus size: 35  
Known-good retained: 14  
Known-bad still rejected: 21  
Unexpected known-good regressions: 0  
Unexpected known-bad approvals: 0  
REVIEW_REQUIRED: 6  
UNSUPPORTED: 9  
Matcher vs catalogue conflicts: 1 (`bad-1x2-mb-k-gamewin-unknown`)

| Archetype | Known-good | Before solver | After approved | REVIEW_REQUIRED | UNSUPPORTED | Conflicts |
|---|---:|---:|---:|---:|---:|---:|
| match_result_1x2 | 3 | 4 | 3 | 2 | 0 | 1 |
| both_teams_to_score | 3 | 3 | 3 | 1 | 0 | 0 |
| total_goals_half_line | 3 | 3 | 3 | 0 | 0 | 0 |
| total_goals_integer | 1 | 1 | 1 | 0 | 1 | 0 |
| first_team_to_score | 3 | 3 | 3 | 2 | 1 | 0 |
| team_total_goals | 0 | 0 | 0 | 1 | 1 | 0 |
| handicap | 0 | 0 | 0 | 0 | 2 | 0 |
| draw_no_bet | 1 | 1 | 1 | 0 | 1 | 0 |
| double_chance | 0 | 0 | 0 | 0 | 1 | 0 |
| team_to_score | 0 | 0 | 0 | 0 | 1 | 0 |
| team_clean_sheet | 0 | 0 | 0 | 0 | 1 | 0 |

The only before/after delta on solver-capable known examples is GAMEWIN-unknown 1X2: matcher+solver-model capability still exists, catalogue `REVIEW_REQUIRED`, production admission blocked. Approved-good complete-regulation 1X2, BTTS, half-line totals, integer MB↔PM totals, 3-state FTTS, and MB↔PM Draw No Bet are retained.

---

## 8. Production admission gate

`scan_eligible_pair` (collector pairing for HOT and UNIVERSE) and `PaperScanService.scan_pair` both require `assess_catalogue_admission(...).allowed`.

Only `APPROVED_EQUIVALENT` is allowed.

Inventory `MATCHED_EQUIVALENT` now means catalogue-approved. Mapping-census `equivalent_market_pairs` remains the collector matcher-pair count; GAMEWIN-unknown 1X2 can still appear there as a structural matcher hit while inventory status is not `MATCHED_EQUIVALENT`.

GAMEWIN-unknown ordinary 1X2 remains a **matcher** structural admission (`ordinary_3way_1x2_kalshi_gamewin_scope_unavailable` on `match_reasons`). It is **not** solver-eligible, **not** paper-admitted, and **not** `MATCHED_EQUIVALENT`.

This PR does **not** remove the matcher narrowing (not a matcher redesign).

---

## 9. Captured / public evidence (not owner-live Matchbook)

Used existing repository captures. No Matchbook credentials were requested or exposed. Credentialed Matchbook live verification remains an owner-Windows acceptance task.

| Source | What it is | Catalogue implication |
|---|---|---|
| `backend/tests/fixtures/polymarket_gamma_event_934146_chelsea_hull.json` | PINNED RAW public Gamma event 934146, retrieved 2026-09-12 | Lone Yes/No moneyline (`Will Chelsea FC win`) with 90-minute wording. Not a 3-way 1X2. Pairing with Matchbook Match Odds is `REVIEW_REQUIRED` (outcome space incomplete / binary vs 3-way). |
| `backend/tests/fixtures/matchbook_event_chelsea_hull.json` | Matchbook-**shaped** discovery fixture, not authenticated live Matchbook | Match Odds + BTTS names are the documented recogniser surface. Not live credentialed evidence. |
| `CAPTURED_PM_DERBY` / `CAPTURED_KALSHI_GAME` in `test_manchester_derby_live_logical.py` | Captured 2026-09-13 public payload **shapes** | Kalshi GAME event has no recoverable regulation-scope wording on the listed market. Cannot approve GAMEWIN 1X2 from ticker/title. |

Optional read-only public spot-check (session 2026-09-17, not CI; no Matchbook credentials):

- Kalshi `GET /events?series_ticker=KXEPLGAME&status=open` returned current EPL games (sample `KXEPLGAME-26SEP18BRECFC` Brentford vs Chelsea). Nested markets carried explicit `after 90 min` wording on Home / Away / Tie. That is recoverable regulation evidence when present. GAMEWIN *template-only* / placeholder-unavailable contracts remain `REVIEW_REQUIRED`. Series ticker still does not approve a pair.
- Polymarket public Gamma `events?slug=epl-bre-che-2026-09-18` (id `967958`) still exposes complementary Yes/No moneylines (`Will Brentford FC win`, draw, `Will Chelsea FC win`), matching the pinned 934146 binary-moneyline shape. A lone binary is not 3-way 1X2. Complementary HOME+DRAW+AWAY Yes contracts can be assembled only when fingerprints agree.

These observations do not approve new venue pairs and are not owner-live Matchbook evidence.

---

## 10. What this PR does not do

- Does not merge scanner-concurrency or paper-autofill work
- Does not change HOT/UNIVERSE scheduling
- Does not enable execution or venue writes
- Does not broaden unsupported matcher/recogniser semantics
- Does not add Get Market enrichment for BTTS / totals / FTTS
- Does not invent team-scope, handicap, double-chance, team-to-score or clean-sheet support
- Does not perform credentialed Matchbook live verification

---

## 11. Tenet review (handoff)

Applicable tenets: 02, 03, 11, 12, 19, 20.

Satisfied:

- 02 paper-only / execution disabled
- 03 incomplete evidence is not catalogue-approved; contradictions and parameter mismatches are hard; only `APPROVED_EQUIVALENT` enters the solver/paper path
- 11 fixture/demo labelled; captured public payloads labelled; not presented as live Matchbook
- 12 this review block
- 19 HOT and UNIVERSE share one catalogue and one admission gate; concurrency unchanged
- 20 pairwise catalogue states for the intended ~10 archetypes; REVIEW_REQUIRED / UNSUPPORTED kept out of solver/paper

Partial / deferred:

- Exception-review workflow / persisted operator rules are specified by Tenet 20 §6–7 but not built
- Team totals remain `REVIEW_REQUIRED` until team-scope exists on `CanonicalMarket` and a solver model exists
- Handicap, Double Chance, Team To Score and Clean Sheet remain unsupported
- Kalshi BTTS/totals/FTTS still lack Get Market enrichment
- Credentialed Matchbook live verification is an owner-Windows acceptance task

Potential conflict:

- Ordinary Matchbook↔Kalshi GAMEWIN-unknown 1X2 still **matches** in `MarketMatcher`. It no longer reaches solver/paper admission. Architect may later remove the matcher narrowing; this PR does not.

Data honesty: deterministic fixture/demo corpus, cited captured public payloads, optional public read-only spot-check. Not owner-live Matchbook.

Safety: paper-only boundary unchanged. `SPORTS_HEDGE_EXECUTION_ENABLED=false`.

Economic correctness: settlement completeness and solver state-space modelling are required for `APPROVED_EQUIVALENT`. Labels and confidence cannot approve.
