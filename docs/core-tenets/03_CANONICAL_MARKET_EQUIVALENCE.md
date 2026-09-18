# Core Tenet 03 — Canonical Market Equivalence

## Principle

Sports Hedge may use tolerant/fuzzy label handling to **recognize a possible supported market**, but it must never fuzzy-match economic settlement semantics.

The operational arbitrage path is deliberately bounded by the **Approved Market Catalogue** in Core Tenet 20.

Two venue markets may be treated as executable equivalents only when:

1. they refer to the same canonical event;
2. both are recognized as the same approved market archetype;
3. all required archetype parameters match;
4. the required outcome state space is economically compatible; and
5. no explicit settlement contradiction exists.

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

Only `APPROVED_EQUIVALENT` may enter the **live-execution** path.

`PAPER_ASSUMED_EQUIVALENT` is an owner-approved Phase-1 **paper-mode** exception for Matchbook↔Kalshi Match Result / 1X2 only. It may enter the paper solver when GAME HOME/DRAW/AWAY is complete, fixture identity is exact, period/line are structurally consistent, and there is no contradictory wording. It carries `settlement_assumption=regulation_time`. It is never represented as independently proven settlement and is never live-execution eligible. Extra time, penalties, to-qualify, fair-price cancellation/reschedule, and incomplete outcome sets remain fail-closed.

If semantics are incomplete or ambiguous but the market plausibly belongs to an approved archetype, and the paper-assumed 1X2 exception does not apply, route it to `REVIEW_REQUIRED`.

Do not silently guess.

Do not admit incomplete settlement to live execution.

Markets outside the approved catalogue are `UNSUPPORTED` for the normal arbitrage path.

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

## Exception review

Ambiguous probable-archetype markets should be preserved as review evidence rather than disappearing.

A review payload may be assessed by GPT/operator workflow.

An approved exception may become a versioned, auditable venue recognition/mapping rule.

Until such approval exists, the market remains non-executable.

## Violation examples

- Joining Arsenal-v-Chelsea odds to a separately generated match ID that historical corners cannot resolve.
- Treating total goals 2.5 as equivalent to total goals 3.5.
- Treating first-half Match Result as full-time Match Result.
- Treating 90-minute Match Result as `to qualify`.
- Treating First Team To Score contracts with materially different no-goal settlement as equivalent.
- Forcing a Polymarket proposition into a football market comparison because the labels look similar.
- Allowing an 88% or 95% generic confidence score to override missing required settlement evidence.
- Sending an unsupported novelty contract to the arbitrage solver.

## Review checks

- Does this change reuse the shared canonical identity contract?
- Is the market inside the approved catalogue?
- Do both venues normalize to the same approved archetype?
- Are required period/line/team/outcome parameters identical?
- Are explicit settlement contradictions hard-blocked?
- Are ambiguous probable-archetype mappings routed to REVIEW_REQUIRED rather than guessed?
- Is a deterministic settlement/equivalence key available before cross-venue comparison?
- Do HOT and UNIVERSE use the same catalogue and equivalence rules?
- Do Research and Arbitrage apply the same proposition-equivalence discipline where they share market contracts?
