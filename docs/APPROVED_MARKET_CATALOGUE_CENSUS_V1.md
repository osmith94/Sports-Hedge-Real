# Approved Market Catalogue Census v1

**Issue:** #268  
**Parent:** R4 `620d1807473a5bfaa08a5023d2a28f4da756efe3`  
**Tenet:** Core Tenet 20 — Approved Market Catalogue and Exception Review  
**Mode:** PAPER MODE · EXECUTION DISABLED  
**Data class:** DETERMINISTIC FIXTURE / DEMO. Not owner-live, not historical quotes, not modelled probabilities.

This census verifies four football archetypes across Matchbook, Kalshi and Polymarket. Approval is **pairwise**. A generic confidence score is never executable permission. Incomplete source evidence is `REVIEW_REQUIRED`. A family that exists in code but cannot be modelled by the solver is not operationally approved.

HOT and UNIVERSE share this catalogue. The classifier has no `scan_lane`. Matcher admission, HOT/UNIVERSE concurrency, paper autofill and execution are unchanged.

Executable matrix source: `backend/src/sports_hedge/catalogue/matrix.py`  
Corpus: `backend/src/sports_hedge/catalogue/corpus.py`  
Classifier: `backend/src/sports_hedge/catalogue/classify.py`

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

`APPROVED_EQUIVALENT` here means: both venues can recognize the archetype, required parameters agree, settlement fingerprints are economically complete and identical, the outcome state space is complete, and Sports Hedge can model every settlement state (simple complete-set or generalized payoff).

---

## 2. Method

1. Read existing venue recognisers, settlement fingerprint construction, matcher gates and solver eligibility at the R4 parent.
2. Do not auto-approve from market labels, series tickers (`KXEPLGAME` / `BTTS` / `TOTAL` / `FTTS`) or confidence.
3. Classify each pair into Tenet 20 states. Incomplete settlement is `REVIEW_REQUIRED` even when `MarketMatcher` currently admits Matchbook↔Kalshi GAMEWIN-unknown ordinary 1X2.
4. Lock known-good and known-bad fixture payloads in a versioned corpus.
5. Report family-level before (current matcher+solver admission) vs after (catalogue state).

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
- Solver: complete regulation 1X2 → simple complete-set. GAMEWIN-unknown currently also enters the simple solver via the documented Matchbook↔Kalshi ordinary-1X2 narrowing. **This census classifies that sibling as REVIEW_REQUIRED.**

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

**Matchbook:** `"total goal"` or (`over under` and `goal`). Participant-id, one named team, or `team total` tokens fail closed to `TEAM_TOTAL`, which is outside this v1 operational four.

**Kalshi:** match totals only. Named-team totals raise (not inferred as match totals). Missing line raises. `line_push_possible is not False` raises: integer/quarter remain deferred. Get Market not fetched. `SOCCERTOTAL.pdf` / `socceranygoal` extra-time default is not applied.

**Polymarket:** match totals vs team-scoped titles (`Total Goals Over 2.5 - Arsenal` is `TEAM_TOTAL`). Integer 2.0 is generalized-eligible when wording is complete.

### 3.4 First Team To Score

Required operational model: `HOME` / `AWAY` / `NO_GOAL`, full time, regulation, extra-time false, penalties false, push false. Solver: generalized payoff (`home_first` / `away_first` / `no_goal`). Two-team books without NO_GOAL are not equivalent (Tenet 20 §9).

**Matchbook:** explicit FTTS phrases, or ambiguous `first goal` only when runners are team-level H/A/NO_GOAL. Missing NO_GOAL still normalizes as FTTS but is incomplete.

**Kalshi:** requires HOME+AWAY+NO_GOAL **and** proven `REGULATION_TIME` or assembly raises. Incomplete FTTS never becomes a CanonicalMarket. Get Market is **not** fetched for FTTS, so nested generic wording cannot be completed today.

**Polymarket:** explicit FTTS or 3 team-level outcomes. Regulation description matches Matchbook convention. Extra-time description is a known contradiction. Player first-goalscorer is `PLAYER_PROPS`, not FTTS.

---

## 4. Explicit REVIEW_REQUIRED examples

| Corpus id | Pair | Why |
|---|---|---|
| `bad-1x2-mb-k-gamewin-unknown` | MB↔K 1X2 | Kalshi GAMEWIN listed scopes; no recoverable `<result scope>`. Catalogue: incomplete settlement. Current matcher still admits this pair into the simple solver (known conflict). |
| `bad-1x2-mb-pm-unknown-settlement` | MB↔PM 1X2 | Polymarket wording does not parse to a complete fingerprint. |
| `bad-btts-k-ambiguous-rules` | MB↔K BTTS | Kalshi `"See contract URL."` is recognized as BTTS but settlement is UNKNOWN. No Get Market enrichment for BTTS. |
| `bad-ftts-missing-no-goal-both` | MB↔PM FTTS | Both books are HOME/AWAY only. 0-0 / void contract is unproven. Solver cannot model the missing state. |
| `bad-ftts-k-unproven-regulation` | MB↔K FTTS | Kalshi FTTS with `"Winner of the match."` does not assemble. |

Also REVIEW_REQUIRED (not all in corpus): lone Polymarket moneyline binary vs 3-way 1X2; Kalshi totals with incomplete nested rules; quarter-line totals (`line_push_possible is None`).

**High confidence does not approve these.** A 0.99 market confidence with UNKNOWN settlement is still `REVIEW_REQUIRED`.

---

## 5. Explicit unsupported / deferred list

**Unsupported for this v1 operational four (ignore or inventory-only):**

- Player first-goalscorer / anytime scorer (`PLAYER_PROPS`)
- Next Goal
- To Qualify
- Draw No Bet (Kalshi deferred until draw-refund proven; DNB is not in this four)
- Double Chance, Asian Handicap, Team Total, Team To Score, Team Clean Sheet (later catalogue targets)
- Corners, cards, correct score, HT/FT, player props
- Matchbook names outside the recogniser (`"Total"`, `"1st Half Total"` without a goals phrase)
- Kalshi titles that do not carry a family phrase (series ticker is not a recogniser)

**Deferred until rules are proven:**

- Kalshi integer/quarter Total Goals (normalizer raises; MB↔K and K↔PM integer cells are `UNSUPPORTED`)
- Kalshi Draw No Bet
- Split/quarter totals on Matchbook/Polymarket (push unknown → `REVIEW_REQUIRED`, not approved)
- Applying `socceranygoal` / `SOCCERTOTAL` extra-time defaults to BTTS or totals (not applied; live selected-clause evidence still required)

---

## 6. Known-good / known-bad corpus

Executable corpus: `sports_hedge.catalogue.corpus.census_corpus()` (26 deterministic fixture entries).

**Known-good (`APPROVED_EQUIVALENT`):**

- 1X2 MB↔PM, MB↔K complete regulation, K↔PM complete regulation
- BTTS MB↔PM, MB↔K complete, K↔PM complete
- Totals 2.5 MB↔PM, MB↔K, K↔PM
- Totals 2.0 MB↔PM (generalized push)
- FTTS 3-state regulation MB↔PM, MB↔K, K↔PM

**Known-bad:**

- GAMEWIN-unknown 1X2 → `REVIEW_REQUIRED`
- Polymarket unknown 1X2 → `REVIEW_REQUIRED`
- First-half vs full-time 1X2 → `APPROVED_PARAMETER_MISMATCH`
- Extra-time vs regulation 1X2 / FTTS → `KNOWN_CONTRADICTION`
- BTTS Kalshi ambiguous rules → `REVIEW_REQUIRED`
- Totals 2.5 vs 3.5 → `APPROVED_PARAMETER_MISMATCH`
- Integer totals MB↔K → `UNSUPPORTED`
- Team total vs match total → `UNSUPPORTED`
- FTTS missing NO_GOAL both sides → `REVIEW_REQUIRED`
- FTTS asymmetric NO_GOAL → `KNOWN_CONTRADICTION`
- Kalshi FTTS without regulation → `REVIEW_REQUIRED`
- Player first-goalscorer vs FTTS → `UNSUPPORTED`

---

## 7. Family-level before / after coverage

Before = R4 `MarketMatcher.matched` **and** `solver_model_for_pair` on the same fixture corpus.  
After = Tenet 20 catalogue state.  
This is not owner-live coverage.

Corpus size: 26  
Known-good retained: 13  
Known-bad still rejected: 13  
Unexpected known-good regressions: 0  
Unexpected known-bad approvals: 0  
REVIEW_REQUIRED: 5  
UNSUPPORTED: 3  
Matcher vs catalogue conflicts: 1 (`bad-1x2-mb-k-gamewin-unknown`)

| Archetype | Known-good | Before solver | After approved | REVIEW_REQUIRED | UNSUPPORTED | Conflicts |
|---|---:|---:|---:|---:|---:|---:|
| match_result_1x2 | 3 | 4 | 3 | 2 | 0 | 1 |
| both_teams_to_score | 3 | 3 | 3 | 1 | 0 | 0 |
| total_goals_half_line | 3 | 3 | 3 | 0 | 1 | 0 |
| total_goals_integer | 1 | 1 | 1 | 0 | 1 | 0 |
| first_team_to_score | 3 | 3 | 3 | 2 | 1 | 0 |

The only before/after delta on solver-admitted known examples is GAMEWIN-unknown 1X2: currently solver-admitted, catalogue `REVIEW_REQUIRED`. Approved-good 3-way complete-regulation 1X2, BTTS, half-line totals, integer MB↔PM totals, and 3-state FTTS are retained.

---

## 8. Known conflict for architect review

**Current matcher still admits Matchbook regulation 1X2 vs Kalshi GAMEWIN-unknown 1X2 into the simple complete-set solver** (`ordinary_3way_1x2_kalshi_gamewin_scope_unavailable`). Core Tenet 20 says incomplete evidence must not become executable equivalence.

This PR does **not** remove that matcher narrowing (not a matcher redesign). The catalogue records it as `REVIEW_REQUIRED` plus `known_conflict_with_current_matcher`. Architect decision: keep the documented Tenet 03 narrowing, or gate solver admission on `APPROVED_EQUIVALENT` only.

---

## 9. What this PR does not do

- Does not merge scanner-concurrency or paper-autofill work
- Does not change HOT/UNIVERSE scheduling
- Does not enable execution or venue writes
- Does not broaden unsupported semantics
- Does not wire the catalogue state into collector pairing yet (deferred until the GAMEWIN conflict is resolved)
- Does not add Get Market enrichment for BTTS / totals / FTTS (would be a later recognition-evidence PR)

---

## 10. Tenet review (handoff)

Applicable tenets: 02, 03, 11, 12, 19, 20.

Satisfied:

- 02 paper-only / execution disabled
- 03 incomplete evidence is not catalogue-approved; contradictions and parameter mismatches are hard
- 11 fixture/demo labelled; not presented as live
- 12 this review block
- 19 HOT and UNIVERSE share one catalogue; concurrency unchanged
- 20 pairwise catalogue states, REVIEW_REQUIRED, corpus, coverage counts

Partial / deferred:

- Solver/paper admission is still `matched` + solver model, not `APPROVED_EQUIVALENT`-gated
- Exception-review workflow / persisted operator rules are specified by Tenet 20 §6–7 but not built
- Remaining six target archetypes (team totals, handicap, DNB, double chance, team to score, clean sheet) are out of this census
- Kalshi BTTS/totals/FTTS still lack Get Market enrichment

Potential conflict:

- Ordinary Matchbook↔Kalshi GAMEWIN-unknown 1X2 remains solver-admitted in the matcher while the catalogue marks it `REVIEW_REQUIRED`

Data honesty: deterministic fixture/demo corpus and code evidence. Not owner-live.

Safety: paper-only boundary unchanged.

Economic correctness: settlement completeness and solver state-space modelling are required for `APPROVED_EQUIVALENT`. Labels and confidence cannot approve.
