# Core Tenet 14 — Event-Driven Price Dislocation Arbitrage

## Principle

A major source of Sports Hedge arbitrage value is expected to come from **temporary cross-venue disagreement after material sporting events**, especially during high-liquidity periods when many matches are being repriced simultaneously.

Sports Hedge should be designed to detect these dislocations quickly and safely across authorised market-data feeds while preserving strict settlement, cost, liquidity and quote-freshness checks.

This is an arbitrage tenet, not a directional prediction tenet.

## Why this matters

Material game events can force rapid repricing:

- red cards;
- goals;
- penalties;
- major injuries;
- goalkeeper injuries / substitutions;
- key-player removal;
- team-sheet shocks;
- other high-impact state changes.

Different venues may react differently because they have different:

- pricing models;
- suspension/reopen policies;
- liquidity providers;
- trader workflows;
- market depth;
- update cadence;
- risk controls.

The opportunity is the **temporary economic disagreement between venues**, not the event itself.

## Non-negotiables

1. Event-driven arbitrage must still pass the same canonical event and settlement-equivalence checks as every other arb.
2. A stale, suspended, crossed or unavailable quote is not executable liquidity.
3. Arbitrage is only recognised when the minimum net payoff across every valid settlement state remains positive after known fees, FX and realistic fill assumptions.
4. The scanner must model executable depth on every required leg, not only top-of-book odds.
5. Quote timestamps, source timestamps and local retrieval timestamps must be retained so the system can distinguish genuine disagreement from stale data.
6. High-impact events should be able to trigger a temporary increase in scan priority/frequency within authorised API limits.
7. High-liquidity windows with many concurrent events should be treated as a first-class operating condition; the system must prioritise rather than assume every market can be polled equally.
8. Event annotations are context. They do not prove that a particular trader, bookmaker or model caused the observed price move.
9. Sports Hedge must not depend on courtsiding, private venue information, broadcast-delay exploitation, geobypass or unauthorised access. The system uses authorised/public data sources and permitted APIs.
10. Phase 1 remains paper-only; event-driven detection must not introduce live venue execution paths.
11. Priority and manual-external workflows should consider **opportunity survivability**, including current price volatility, rather than ranking opportunities by current edge alone.

## Event-driven burst mode

The architecture should support an **event-driven burst mode** or equivalent priority scheduler.

Possible triggers include:

```text
RED_CARD
GOAL
PENALTY
PLAYER_INJURY
KEY_PLAYER_SUBSTITUTED
TEAM_SHEET
ABNORMAL_CROSS_VENUE_SPREAD
RAPID_PRICE_MOVE
VENUE_REOPEN_AFTER_SUSPENSION
```

When triggered, Sports Hedge may temporarily:

- increase snapshot frequency for the affected canonical event;
- prioritise the most liquid/economically comparable markets;
- re-run cross-venue matching and arb calculations;
- monitor near-arb candidates more frequently;
- track which venues have suspended, reopened or materially repriced;
- raise Priority Arb Alerts when a valid exceptional opportunity appears.

The scheduler must stay within venue rate limits and configured cost constraints.

## Reaction-latency research

Sports Hedge should measure market-reaction timing historically where reliable timestamps exist.

Useful outputs include:

```text
venue suspension timestamp
venue reopen timestamp
first material price move
first stable post-event quote
cross-venue dispersion peak
cross-venue convergence time
near-arb duration
validated-arb duration
executable depth during dislocation
```

These metrics are for system design and historical research. They must not be treated as proof that the same timing will persist.

## Opportunity survivability

Sports Hedge should estimate how likely an identified opportunity is to **remain economically valid long enough to act on it**.

Survivability is distinct from current net edge and from fill confidence. It is an operational forecast based on the state and recent behaviour of the market.

Important inputs should include, where available:

```text
recent realised price volatility on every required leg
rate and direction of recent price change
cross-venue dispersion velocity / convergence speed
quote persistence and quote age
spread and order-book depth
rate of depth cancellation / replenishment
number of book levels required
venue suspension / reopen behaviour
recent event-driven repricing state
historical duration of comparable dislocations
expected operator / counterparty response time for MANUAL_EXTERNAL legs
```

Volatility must materially affect the estimate. A 1% current arbitrage in a calm, persistent book should normally be assessed as more survivable than the same 1% edge while one or both legs are repricing rapidly.

Useful outputs may include:

```text
survivability_score                  # calibrated 0-100 operational score
survival_probability_5s
survival_probability_15s
survival_probability_30s
survival_probability_60s
estimated_median_remaining_life
historical_dislocation_half_life
volatility_regime
adverse_move_rate
```

The exact model can evolve, but the underlying inputs and calibration must be inspectable. Do not manufacture precision where historical coverage is weak.

For a manual-external opportunity, Sports Hedge should compare estimated survivability with the expected confirmation latency. For example, if an external counterparty normally needs 30 seconds and the opportunity has a very low estimated probability of remaining valid for 30 seconds, the alert should be downgraded or explicitly marked **LOW SURVIVABILITY**, even if the current headline edge is attractive.

Survivability is not a guarantee. Any confirmed external fill must still trigger a fresh hedge revalidation before the remaining leg can proceed.

## High-liquidity concurrency

The product must work when many games are live simultaneously.

Examples include:

- Premier League Saturday 15:00 windows;
- Champions League multi-match windows;
- future supported high-liquidity sports such as NBA game clusters.

When capacity is constrained, prioritisation should consider:

```text
market liquidity
current cross-venue dispersion
near-arb distance to trigger
quote freshness
number of equivalent venues available
execution-risk score
opportunity survivability / volatility regime
historical event-response relevance
```

Do not prioritise merely by headline odds movement.

## Relationship to Research

Research may help explain or anticipate which events tend to create large market reactions, but the Arbitrage engine must not require the Research model to believe an event is mispriced.

The Arbitrage question remains:

> Do economically equivalent, executable venue prices currently produce a positive minimum payoff after costs?

The Research question is different:

> How do markets and teams historically respond to this type of event?

They may share event timestamps and canonical identities but must remain separate analytical paths.

## Priority Alerts

Event-driven dislocations are a natural source of **Priority Arb Alerts**.

A large headline edge is not sufficient. Escalation should also require:

- fresh executable quotes;
- substantial depth on all legs;
- acceptable fill/execution risk;
- known fees/FX;
- strict settlement equivalence;
- sufficient expected guaranteed profit / capital efficiency;
- survivability appropriate to the expected action/confirmation latency.

## Violation examples

This tenet is violated if Sports Hedge:

- treats a pre-event stale quote as a live arb after a red card;
- assumes that a suspended venue quote can be filled;
- ignores quote age during a rapid event-driven market move;
- treats a highly volatile fleeting edge as equivalent to a persistent edge without a survivability distinction;
- polls low-value markets while missing highly liquid active dislocations because there is no prioritisation;
- conflates a strong Research prediction with guaranteed arbitrage;
- relies on unauthorised or courtside latency advantages;
- claims a human trader caused a price difference without evidence.

## Review checks

For any event-driven arbitrage implementation, reviewers should verify:

- [ ] event-to-market prioritisation exists or has a clear seam;
- [ ] quote freshness is explicit and enforced;
- [ ] suspended/unavailable/stale quotes cannot be treated as executable;
- [ ] depth is validated across every required leg;
- [ ] fees and FX remain fail-closed;
- [ ] canonical settlement equivalence is unchanged;
- [ ] event timestamps and venue quote timestamps are retained separately;
- [ ] burst scanning respects API/rate-limit constraints;
- [ ] concurrent-game prioritisation is deterministic and inspectable;
- [ ] Priority Alert escalation uses execution quality as well as edge;
- [ ] survivability incorporates current volatility and expected action latency;
- [ ] survivability inputs/calibration remain inspectable and uncertainty is explicit;
- [ ] no live order placement is introduced in Phase 1;
- [ ] no courtsiding, geobypass or unauthorised latency exploitation is required.

## Guiding question

Sports Hedge should be able to answer:

> A major event just changed this game. Which economically equivalent venues have repriced, which have not, is there a genuinely executable cross-venue arbitrage after costs right now, and how likely is that opportunity to survive long enough to act on it?
