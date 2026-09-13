# Core Tenet 18 — Execution Atomicity and Fill Risk

## Principle

A mathematically valid arbitrage is not operationally risk-free unless every required execution leg is captured as intended.

The principal transition risk from paper trading to real-money execution is **legging risk**: one venue fills while another venue is delayed, suspended, repriced, partially filled or rejected. At that point the position is no longer guaranteed arbitrage and may become directional market exposure.

Paper-mode success therefore proves the economics and lifecycle, but it must never be treated as proof that simultaneous real fills are achievable.

## Principal risk scenarios

Sports Hedge must assume that any real execution attempt can encounter:

- one leg filling before the other;
- a venue suspending immediately after a goal, red card or other material event;
- a quote disappearing between detection and order arrival;
- partial fill on one or more legs;
- venue-side execution delay;
- a resting remainder after only part of an intended hedge fills;
- price movement that destroys the arb after the first fill;
- API rejection, throttling, cancellation or temporary unavailability;
- differences in venue pause/reopen behaviour;
- external/manual counterparty latency.

A stale or previously observed quote is never evidence that the remaining leg can still be filled.

## Non-negotiables before real treasury execution

1. **No paper-to-live equivalence claim.** Realistic paper simulation may model latency, slippage, depth, stale quotes and partial fills, but it is still a model.
2. **Every fill is state-changing.** After any real or externally confirmed fill, all remaining legs must be revalidated against fresh executable prices, depth, fees, FX and market status before further execution.
3. **No guaranteed label after a broken hedge.** If all required legs are not completed, the position must be marked as partial/unhedged exposure rather than arbitrage.
4. **Hard exposure limits.** Maximum unhedged exposure, maximum time unhedged and maximum recovery loss must be configured independently of normal arb sizing.
5. **Suspension awareness.** Venue paused/suspended/unavailable states must block fresh execution and influence whether an already-filled leg should be hedged, reduced or exited elsewhere.
6. **Idempotent recovery.** Retries must never duplicate filled exposure.
7. **Kill switch.** Real execution must be able to halt immediately when fill behaviour, venue status or reconciliation becomes unreliable.

## Candidate mitigations

Sports Hedge should support and empirically test execution controls such as:

- Fill-or-Kill or Immediate-or-Cancel instructions where supported and appropriate;
- adaptive or staggered execution in smaller chunks rather than exposing the full intended size at once;
- re-solving and re-sizing the remaining hedge after each confirmed chunk;
- choosing leg order based on venue liquidity, latency, suspension behaviour and hedgeability;
- cancelling unfilled remainder promptly where venue semantics permit;
- pre-trade reduction of size in high-volatility / low-survivability situations;
- stronger limits around event shocks such as goals and red cards;
- emergency hedge or flatten logic for a filled first leg when the intended second leg becomes unavailable;
- explicit manual fallback where automation cannot safely restore neutrality.

Chunking is a mitigation, not a universal rule. Smaller increments reduce the amount exposed by a failed second leg, but increase total execution time and the number of opportunities for the market to move. The system should optimise chunk size rather than assume smaller is always safer.

## Shadow execution gate

Before meaningful real capital is enabled, Sports Hedge should run a **shadow execution phase** using live read-only market data.

For each qualifying opportunity it should record, without sending orders:

- detection timestamp;
- intended leg order and size;
- quoted executable price/depth;
- expected venue latency;
- whether each leg would still have been executable after representative delays;
- suspension/reopen events;
- price/depth decay after 100 ms, 250 ms, 500 ms, 1 s and other useful windows;
- whether the full hedge would have completed;
- maximum temporary unhedged exposure;
- realised hypothetical edge after observed latency.

The purpose is to estimate actual **dual-fill / full-hedge capture probability** rather than assume it from static books.

## Micro-live gate

Any future move beyond shadow mode should begin with deliberately small real exposure and hard caps.

The objective is not early profit maximisation. It is to measure:

- order acknowledgement latency;
- fill latency;
- partial-fill frequency;
- suspension-related failure rate;
- cancel/reject behaviour;
- hedge recovery effectiveness;
- realised slippage versus paper assumptions;
- percentage of detected arbs that become fully hedged positions.

Treasury scale should increase only after observed fill behaviour supports it.

## Relationship to opportunity survivability

Current net edge alone is insufficient for execution sizing.

Future execution decisions should distinguish:

- **economic edge** — expected net arbitrage ROI after fees/costs;
- **survivability** — probability the opportunity remains valid long enough to act;
- **fill confidence** — probability each individual leg fills as intended;
- **full-hedge capture probability** — probability the complete multi-leg hedge is achieved before the opportunity disappears.

A high-edge opportunity with low capture probability may warrant zero live size.

## Review checks

Before any real-money execution capability is approved, reviewers should verify:

- [ ] paper-mode results are not being used as evidence of simultaneous live fill;
- [ ] each confirmed fill triggers fresh remaining-hedge validation;
- [ ] partial/unhedged states are explicit and auditable;
- [ ] suspension and venue-unavailable states block unsafe continuation;
- [ ] maximum unhedged exposure and duration are hard-limited;
- [ ] execution strategy considers IOC/FOK or equivalent venue controls where available;
- [ ] adaptive/staggered sizing can be tested without assuming it always improves outcomes;
- [ ] retries and recovery are idempotent;
- [ ] a kill switch exists;
- [ ] shadow execution evidence exists before significant real treasury is enabled;
- [ ] micro-live evidence exists before treasury size is materially increased;
- [ ] full-hedge capture probability is measured empirically rather than assumed.

## Guiding question

Sports Hedge should be able to answer:

> If the first leg fills and the market changes before the remaining legs complete, exactly how much exposure can we have, for how long, and what deterministic action restores or limits the position?
