# Core Tenet 03 — Canonical Market Equivalence

## Principle

Sports Hedge may use tolerant/fuzzy label handling to **recognize a possible supported market**, but it must never fuzzy-match economic settlement semantics.

The operational arbitrage path is deliberately bounded by the **Approved Market Catalogue** in Core Tenet 20 and the **Approved Match Register** (Issue #331).

Runtime scanning must not score, review, learn, or re-litigate whether an owner-approved venue market pair is equivalent. Equivalence is decided during venue onboarding and stored in the register. The scanner consumes that truth deterministically.

Two venue markets may be treated as paper-mode equivalents when:

1. they refer to the same canonical event;
2. each side maps through a venue-native archetype to the same canonical key;
3. all required archetype parameters match (period, line, outcome space).

Extra-time, penalties, and to-qualify contracts are different native archetypes and do not receive a full-time register key. The scanner does not re-litigate settlement fingerprints after a registered key is resolved.

A generic confidence percentage is not permission to treat ambiguous contracts as economically equivalent.

## Non-negotiables

Before comparing prices or constructing arbitrage/value outputs, validate as applicable:

- sport;
- canonical event identity;
- normalized participants;
- kickoff time;
- approved market archetype;
- period;
- line/handicap;
- participant/team scope;
- outcome state space;
- extra-time/penalty treatment;
- no-goal/draw treatment where applicable;
- void/postponement rules where material;
- canonical settlement/equivalence key.

The normal operational states should distinguish:

```text
APPROVED_EQUIVALENT
PAPER_ASSUMED_EQUIVALENT
APPROVED_PARAMETER_MISMATCH
KNOWN_CONTRADICTION
REVIEW_REQUIRED
UNSUPPORTED
```

Only independently proven `APPROVED_EQUIVALENT` rows that are also in the
Approved Match Register may enter the **live-execution** path. Unregistered
independently proven fingerprints may still classify `APPROVED_EQUIVALENT` for
offline census/onboarding; they do not grant runtime matcher or paper-solver
admission until that venue-native archetype is added to the register.

`PAPER_ASSUMED_EQUIVALENT` is an owner-approved Phase-1 **paper-mode** path for Matchbook↔Kalshi rows in the Approved Match Register: MATCH_RESULT / 1X2, BTTS, exact-line TOTAL_GOALS, and FIRST_TEAM_TO_SCORE. The register itself is the PAPER cross-venue equivalence decision. Once canonical fixture identity and canonical market identity/parameters match, the scanner must not re-litigate settlement wording, mapping confidence, or learned labels on every scan. It carries `settlement_assumption=regulation_time` and an owner-approved paper-equivalence / register audit marker. It is never represented as independently proven settlement and is never live-execution eligible.

Kalshi cancel/reschedule-to-fair-price wording does **not** block PAPER admission for these four families. Extra time, penalties, and to-qualify are different/unregistered native archetypes at register mapping — they do not receive a full-time GAME/BTTS/TOTAL/FTTS key, and the scanner does not veto a registered row by re-reading settlement fingerprints. Wrong fixture/family/period, TOTAL line mismatch, and incomplete/incorrect outcome-space identity remain fail-closed through the register structural gate. Genuinely absent venue markets stay NOT_LISTED / VENUE_UNAVAILABLE and must not be invented.

If semantics are incomplete or ambiguous but the market is **not** a registered structural match, do not admit it. Unregistered/unsupported markets are ignored by the scanner. Parameter/structural mismatch is rejected. Genuinely absent markets stay NOT_LISTED / VENUE_UNAVAILABLE.

Do not silently guess.

Do not admit incomplete settlement to live execution.

Markets outside the approved catalogue/register are `UNSUPPORTED` for the normal arbitrage path.

## Canonical event identity

The same canonical football match identity should join:

```text
historical facts
historical odds
live market data
research scenarios
arbitrage opportunities
```

Fixture identity may use scored evidence where appropriate, but explicit identity contradictions remain hard vetoes.

## Non-negotiable economic test

An arbitrage exists only when the minimum **net** payoff across every valid settlement state is positive after modeled costs.

Research may compare a model with a market price only where the market proposition is economically equivalent to the proposition being modelled.

## Approved catalogue relationship

Core Tenet 20 defines which market contracts Sports Hedge deliberately supports in the normal scanner.

This tenet defines the safety rule inside that catalogue:

> **same approved archetype + same required parameters + compatible settlement states, or no executable equivalence**

A venue title or ticker may be recognized through an operator-approved venue rule, but the resulting canonical contract must still pass this equivalence test.

## Exception review (onboarding, not runtime scanning)

Ambiguous probable-archetype markets may be preserved as onboarding evidence rather than disappearing.

A review payload may be assessed by GPT/operator workflow **during venue onboarding**. An approved exception becomes a versioned Approved Match Register entry.

Until such approval exists, the market remains unregistered and is not admitted by the scanner. Runtime scanning does not consult mapping confidence, learned market-label rules, or the dynamic mapping-review store to decide equivalence for registered rows.

## Violation examples

- Joining Arsenal-v-Chelsea odds to a separately generated match ID that historical corners cannot resolve.
- Treating total goals 2.5 as equivalent to total goals 3.5.
- Treating first-half Match Result as full-time Match Result.
- Treating 90-minute Match Result as `to qualify`.
- Treating First Team To Score contracts with materially different no-goal settlement as equivalent.
- Forcing a Polymarket proposition into a football market comparison because the labels look similar.
- Allowing an 88% or 95% generic confidence score to override missing required settlement evidence or to grant register admission.
- Sending an unsupported novelty contract to the arbitrage solver.
- Re-litigating a registered Matchbook↔Kalshi row on every scan because Kalshi fair-price wording differs.

## Review checks

- Does this change reuse the shared canonical identity contract?
- Is the market inside the approved catalogue?
- Do both venues normalize to the same approved archetype?
- Are required period/line/team/outcome parameters identical?
- Are explicit settlement contradictions hard-blocked?
- Are ambiguous unregistered markets kept out of the solver rather than guessed?
- Is a deterministic register canonical key available before paper comparison?
- Do HOT and UNIVERSE consume the same Approved Match Register?
- Do Research and Arbitrage apply the same proposition-equivalence discipline where they share market contracts?
