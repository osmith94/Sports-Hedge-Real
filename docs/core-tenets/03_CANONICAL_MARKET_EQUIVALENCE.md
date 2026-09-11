# Core Tenet 03 — Canonical Market Equivalence

## Principle

Sports Hedge may fuzzy-match labels for discovery, but it must never fuzzy-match economic settlement semantics.

Two markets can only be treated as equivalent when canonical event identity and settlement fingerprint are sufficiently complete and compatible.

## Non-negotiables

Before comparing prices or constructing arbitrage/value outputs, validate as applicable:

- sport;
- competition;
- normalized participants;
- kickoff time;
- market family;
- period;
- line/handicap;
- outcome state space;
- extra-time/penalty treatment;
- void/postponement rules;
- canonical settlement fingerprint.

If semantics are incomplete or ambiguous, fail closed.

The same canonical football match identity should join:

```text
historical facts
historical odds
live market data
research scenarios
arbitrage opportunities
```

## Non-negotiable economic test

An arbitrage exists only when the minimum **net** payoff across every valid settlement state is positive after modeled costs.

Research may compare a model with a market price only where the market proposition is economically equivalent to the proposition being modelled.

## Violation examples

- Joining Arsenal-v-Chelsea odds to a separately generated match ID that historical corners cannot resolve.
- Treating two similarly named totals markets with different settlement rules as equivalent.
- Forcing a Polymarket proposition into a football market comparison because the labels look similar.

## Review checks

- Does this change reuse the shared canonical identity contract?
- Are ambiguous mappings rejected rather than guessed?
- Is a deterministic settlement/equivalence key available before cross-venue comparison?
- Do Research and Arbitrage apply the same equivalence discipline?
