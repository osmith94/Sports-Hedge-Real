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

## Executable taker liquidity vs passive/maker liquidity

Fill risk is side-aware. Sports Hedge must distinguish **executable/taker liquidity** from **passive/resting maker liquidity**. This distinction is part of the product contract, not an implementation footnote.

### Freshness is revalidation, not price movement

- A quote being unchanged is not itself stale. Freshness is based on timestamp and revalidation against the current book, not on whether the displayed price moved.
- Fresh-but-unchanged opposing depth may still be executable if the venue is actively refreshing the book and the quote age is within the absolute freshness limit.
- A stale or previously observed quote is never executable evidence until it has been revalidated against the current book.
- Do not waive absolute quote-age/freshness limits merely because a stale-looking price is favourable.

### Executable / taker liquidity

**Executable/taker liquidity** means there is current opposing depth we can consume now at a known price and size.

A freshly revalidated stale-looking price may be attractive if we can actually take it immediately and the hedge remains executable. Consuming current taker depth is still subject to depth, fee/FX, risk and absolute quote-age gates.

Executable taker depth is the only liquidity that may:

- count toward executable depth or solver-qualified opportunity size;
- qualify an opening arb;
- become the fixture-row headline `Best Arb`.

### Passive / maker liquidity

**Passive/maker liquidity** means our strategy would require posting an order or lay and waiting for another participant to trade into it.

That is not guaranteed or executable liquidity. It must not be treated as current fillable size.

If an arb depends on posting a passive lay/maker order — especially at a stale or lagging price — it must **fail closed** as an executable arb until an actual fill occurs. Do not merely apply a small risk penalty or down-weight.

Passive or stale maker liquidity must never:

- become the fixture-row headline `Best Arb`;
- count toward executable depth;
- count toward solver-qualified opportunity size;
- be labelled qualifying.

A mathematically attractive maker/passive or stale candidate may appear in drilldown as observed edge / not executable, with the exact rejection reason. It must not outrank a lower-edge genuinely executable taker opportunity on the fixture summary.

### Phase-1 opening path

The current Phase-1 opening arb path remains **back/buy only**.

Matchbook lay levels may be observed for risk and market intelligence. They do not enter opening-arb qualification, executable depth, or fixture-headline `Best Arb` selection.

### Future opening lay / maker execution

Any future support for opening lay/maker execution requires a **separate execution model** before it can qualify as executable. That model must include:

- fill probability;
- queue position / time-in-market;
- cancellation and repricing;
- side-aware freshness (stale or materially lagging passive quotes fail closed immediately);
- shadow execution evidence;
- Tenet-18 recovery controls (revalidation after every fill, unhedged-exposure limits, idempotent recovery, kill switch).

Until that model exists, posting a passive order is not an executable opening arb.

## Non-negotiables before real treasury execution

1. **No paper-to-live equivalence claim.** Realistic paper simulation may model latency, slippage, depth, stale quotes and partial fills, but it is still a model.
2. **Every fill is state-changing.** After any real or externally confirmed fill, all remaining legs must be revalidated against fresh executable prices, depth, fees, FX and market status before further execution.
3. **No guaranteed label after a broken hedge.** If all required legs are not completed, the position must be marked as partial/unhedged exposure rather than arbitrage.
4. **Hard exposure limits.** Maximum unhedged exposure, maximum time unhedged and maximum recovery loss must be configured independently of normal arb sizing.
5. **Suspension awareness.** Venue paused/suspended/unavailable states must block fresh execution and influence whether an already-filled leg should be hedged, reduced or exited elsewhere.
6. **Idempotent recovery.** Retries must never duplicate filled exposure.
7. **Kill switch.** Real execution must be able to halt immediately when fill behaviour, venue status or reconciliation becomes unreliable.
8. **Taker vs maker.** Only current opposing taker depth is executable opening liquidity. Passive/maker size is not executable until a dedicated execution model and an actual fill exist.

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
- whether each leg is taker (consume now) or would require posting/maker;
- expected venue latency;
- whether each leg would still have been executable after representative delays;
- suspension/reopen events;
- price/depth decay after 100 ms, 250 ms, 500 ms, 1 s and other useful windows;
- whether the full hedge would have completed;
- maximum temporary unhedged exposure;
- realised hypothetical edge after observed latency.

The purpose is to estimate actual **dual-fill / full-hedge capture probability** rather than assume it from static books. Shadow evidence for a passive/maker leg is not taker-fill evidence.

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

Fixture-row `Best Arb` is the best **fully executable/qualified** opportunity after depth, risk, fill-confidence, freshness and fee/FX gates, not the largest raw or net mathematical edge. If none qualify, prefer the best near opportunity that still satisfies executable-depth and risk requirements. Otherwise show `No executable arb` rather than a misleading headline edge. All market-level comparisons remain visible in drilldown.

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
- [ ] full-hedge capture probability is measured empirically rather than assumed;
- [ ] freshness is based on timestamp/revalidation, not on whether the price moved;
- [ ] a stale or previously observed quote cannot count as executable until revalidated;
- [ ] executable/taker liquidity means current opposing depth we can consume now at known price/size;
- [ ] passive/maker liquidity (post and wait) is not treated as guaranteed or executable size;
- [ ] an arb that depends on posting a passive lay/maker order fails closed until an actual fill, not a small risk penalty;
- [ ] passive or stale liquidity cannot become fixture-row `Best Arb` or count toward executable depth / solver-qualified size;
- [ ] Phase-1 opening remains back/buy only; Matchbook lay levels may be observed but do not qualify opening arbs;
- [ ] any future opening lay/maker path has a separate execution model (fill probability, queue/time-in-market, cancel/reprice, shadow evidence, Tenet-18 recovery) before it can qualify;
- [ ] a stale/passive high-edge candidate cannot outrank a lower-edge genuinely executable taker opportunity.

## Guiding question

Sports Hedge should be able to answer:

> If the first leg fills and the market changes before the remaining legs complete, exactly how much exposure can we have, for how long, and what deterministic action restores or limits the position?

And for every displayed opening opportunity:

> Is this current opposing depth we can take now, or would we have to post and wait — and if we would have to post, why is this still being treated as executable?
