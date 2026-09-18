# Phase-1 common-market catalogue census v3 (Issue #316)

**Base:** current owner-live `46d20dd2e1f64a7445ca70bfcdcc184525e26485`  
**Registry:** `backend/src/sports_hedge/catalogue/registry.py` (`REGISTRY_VERSION=v3`)  
**Team registry:** `backend/src/sports_hedge/facts/team_registry.py`  
**Tenet:** Core Tenet 20, with Core Tenet 03 remaining settlement-safety authority. The 1X2 paper-mode assumption is an explicit owner-approved exception.  
**Mode:** PAPER MODE · EXECUTION DISABLED  
**Data class:** DETERMINISTIC REGISTRY from owner-live code, tests, captured public payloads, and documented read-only series metadata. Not live quotes. Not modelled probabilities.

HOT and UNIVERSE consume the same registry. HOT targeted refresh (#320) persists exact venue market IDs for both `APPROVED_EQUIVALENT` and `PAPER_ASSUMED_EQUIVALENT` rows.

This census does **not** invent Kalshi availability or settlement semantics.

---

## 1. Phase-1 operational target (exactly four families)

For each genuinely offered Matchbook↔Kalshi fixture, UNIVERSE attempts these four canonical opportunities:

| Family | Paper-mode admission | Live-execution | Notes |
|---|---|---|---|
| MATCH_RESULT / 1X2 | `PAPER_ASSUMED_EQUIVALENT` when GAME HOME/DRAW/AWAY is complete and there is no contradictory wording | never, unless independently proven `APPROVED_EQUIVALENT` | `settlement_assumption=regulation_time` |
| BTTS | `APPROVED_EQUIVALENT` when proven | never in Phase 1 (execution disabled) | independent settlement proof required |
| TOTAL_GOALS (safe half-line) | `APPROVED_EQUIVALENT` when exact line matches | never in Phase 1 | 2.5↔2.5 yes; 2.5↔3.5 no; integer/quarter deferred |
| FTTS | `APPROVED_EQUIVALENT` when 3-state regulation is proven | never in Phase 1 | no player first-goalscorer confusion |

Example fixture diagnostics:

```text
MATCH_RESULT  | PAPER_ASSUMED_EQUIVALENT | GAME HOME/DRAW/AWAY complete | regulation-time assumption
BTTS          | APPROVED_EQUIVALENT       | proven
TOTAL_GOALS   | APPROVED_EQUIVALENT       | exact line 2.5 | proven
FTTS          | APPROVED_EQUIVALENT       | proven
DNB           | VENUE_UNAVAILABLE
```

---

## 2. Ten-archetype Matchbook↔Kalshi truth table

Layers are separate. No archetype becomes live-execution eligible just to increase count.

| Archetype | TARGET | Phase-1 four-family | Matchbook recognizer | Kalshi recognizer / series | Settlement proof | Solver | Current MB↔K admission | Exact reason when not operational | Evidence |
|---|---|---|---|---|---|---|---|---|---|
| 1X2 | yes | yes | Match Odds / Match Result | `*GAME`; YES HOME/DRAW/AWAY | Proven only from market-specific 90-minute wording. GAMEWIN template is **not** independently proven | simple complete-set | `PAPER_ASSUMED_EQUIVALENT` when H/D/A complete and no contradiction; `APPROVED_EQUIVALENT` when 90-minute wording independently proves regulation | Extra time / penalties / to-qualify / fair-price cancel → fail closed. Incomplete H/D/A → not paper-assumed. Series ticker never approves. | Census v1 §3.1/§9; owner #316 paper-mode contract |
| BTTS | yes | yes | `both teams to score` / `btts` | `*BTTS` | Proven when market rules prove regulation | simple complete-set | `APPROVED_EQUIVALENT` when instance complete | Ambiguous Kalshi rules → `REVIEW_REQUIRED` | Census v1 §3.2; owner-live #301 |
| TOTAL half-line | yes | yes | total goal / over-under goal | `*TOTAL`; half-line only | Proven when half-line rules prove regulation | simple complete-set | `APPROVED_EQUIVALENT` when exact line matches | Line mismatch → `APPROVED_PARAMETER_MISMATCH`. Integer/quarter Kalshi totals not assembled | Census v1 §3.3 |
| TOTAL integer | yes | no | integer Over/Under | **unavailable** | n/a on Kalshi | generalized on MB↔PM only | `VENUE_UNAVAILABLE` MB↔K | Kalshi integer/quarter totals deferred | Census v1 §3.3 |
| FTTS | yes | yes | FTTS / first goal with 3-state runners | `*FTTS` except Championship and Intl friendlies | Proven only with NO_GOAL **and** `REGULATION_TIME` | generalized payoff | `APPROVED_EQUIVALENT` when instance complete | Missing NO_GOAL / unproven regulation → `REVIEW_REQUIRED` | Census v1 §3.4 |
| TEAM TOTAL | yes | no | recognized | **unavailable** | unproven | none | `VENUE_UNAVAILABLE` MB↔K | No Kalshi team-total series. Not a Phase-1 expensive-work family | Census v1 §3.5 |
| HANDICAP | yes | no | recognized, unproven | **unavailable** | unproven | none | `VENUE_UNAVAILABLE` MB↔K | No Kalshi handicap series | Census v1 §3.6 |
| DNB | yes | no | recognized | **unavailable** | n/a on Kalshi | generalized on MB↔PM only | `VENUE_UNAVAILABLE` MB↔K | No Kalshi DNB series. Must not trigger depth/solver | Census v1 §3.7 |
| DOUBLE CHANCE | yes | no | recognized, no solver | **unavailable** | unproven | none | `VENUE_UNAVAILABLE` MB↔K | No Kalshi DC series | Census v1 §3.8 |
| TEAM TO SCORE | yes | no | recognizer missing | **unavailable** | n/a | none | `VENUE_UNAVAILABLE` MB↔K | Not First Team To Score | Census v1 §3.9 |
| CLEAN SHEET | yes | no | recognizer missing | **unavailable** | n/a | none | `VENUE_UNAVAILABLE` MB↔K | No recogniser / series | Census v1 §3.10 |

---

## 3. Efficient UNIVERSE mechanism

1. Discover fixtures only inside `TARGET_COMPETITIONS`.
2. Canonicalise team names from `facts/team_registry.py`. Unknown / youth / women / reserve remainders fail closed.
3. Match fixture identity once: target competition + canonical home + canonical away + kickoff. Sibling Kalshi GAME/BTTS/TOTAL/FTTS containers share one canonical fixture row.
4. Inspect only the four Phase-1 families for operational work. Kalshi uses GAME/BTTS/TOTAL/FTTS series only. Matchbook parses broader inventory once and discards non-target families immediately (`phase1_non_target_family_discarded`).
5. Expensive settlement / paper-assumption / depth / solver work runs only when both venues expose the same canonical family and parameters.
6. Persist the exact venue market relationship so HOT (#320) refreshes those IDs only.

---

## 4. Team identity registry

`facts/team_registry.py` is keyed by `TARGET_COMPETITIONS`:

- Premier League (20 senior clubs + existing aliases)
- Championship (current + Football-Data England seeds)
- La Liga (senior-club set + existing Athletic Club / Espanyol / Elche aliases)
- Bundesliga (senior-club set + existing Bayern / Union Berlin aliases)
- Serie A (senior-club set + existing Milan / Monza / Sassuolo aliases)
- Carabao Cup / FA Cup reuse English senior clubs
- International friendlies: senior national teams for identity only

`Brentford FC` / `Chelsea FC` resolve to the same canonicals as `Brentford` / `Chelsea`. Unknown FC remainders stay unstripped.

---

## 5. What this census does not claim

- Credentialed owner-live Matchbook quotes
- That Kalshi offers DNB/handicap/DC/team-score/clean-sheet because the product catalogue wants them
- That GAMEWIN template 1X2 is independently settlement-proven
- That paper-assumed 1X2 is live-execution eligible
- Execution enabled / venue writes
