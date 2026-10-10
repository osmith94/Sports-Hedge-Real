# Core Tenet 21 — Conservative Market Making and Cross-Venue Hedging

## Principle

Sports Hedge may develop a **separate MARKET MAKER strategy** that posts resting **LAY** liquidity on Matchbook and monitors an equivalent Polymarket hedge through STREAM. This is an experiment in *providing missing Matchbook liquidity*, not simply finding pre-existing two-sided taker arbitrage.

> **Discover gaps through UNIVERSE. Observe through STREAM. Quote conservatively. Reprice or withdraw unfilled Matchbook offers when the reference moves. Hedge confirmed Matchbook fills promptly. Never call an unmatched maker offer a guaranteed arb.**

A target quoted margin is conditional on both the lay being matched and the hedge executing. It is **not a guaranteed profit** while an offer rests. **Passive-maker fill behaviour cannot be established from paper-market data alone**: genuine fill probabilities, cancellation races and actual fill-to-hedge outcomes require observations from real, exchange-acknowledged orders. Consequently, after read-only shadow validation and a separate live-readiness gate, the intended empirical trial is **explicitly authorised, tiny real-money maker orders**, not an obligatory maker paper-trading performance phase. This tenet is a future design contract, **not current authorisation to place venue orders** or turn on real execution.

## 1. UNIVERSE is a candidate source, including unmatched fixtures

UNIVERSE should supply a **Market-Making Candidate Radar** that considers:

- Registered matched venue pairs with thin Matchbook depth, wide spreads, limited competing offers or infrequently updated Matchbook odds.
- **Unmatched UNIVERSE fixtures / unpaired venue observations**, specifically to investigate whether they expose a neglected market-making opportunity.
- Markets where there is a real Matchbook native event and LAY market, but little active market-maker competition.
- Fixtures where Polymarket has an equivalent outcome with sufficiently deep, liquid, fresh executable BUY books, even though existing Matchbook taker quotes do not produce a conventional arbitrage.

**A fixture being unmatched is a research signal, not permission to trade.** Keep separate statuses:

1. **CANDIDATE / REVIEW:** any UNIVERSE fixture or observed native market can appear, including unmatched/missing mappings; no maker quote.
2. **BLOCKED / UNAVAILABLE:** missing Matchbook market, no Polymarket hedge market, unmatched/contradictory canonical settlement, insufficient Polymarket depth or outdated data; no maker quote.
3. **APPROVED FOR SHADOW:** exact canonical fixture, venue-native IDs, period, contract parameters, all outcomes and settlement rules are already authorised under the **existing Approved Match Register** (Core Tenets 03/20); no real offer.
4. **LIVE-ELIGIBLE:** only in a separately approved future execution stage after order-lifecycle, capital, venue-permissions and risk gates.

Do not fabricate a Matchbook listing if one does not exist. Do not let a vague label, high matching confidence, an unregistered market, or a 'stale' Matchbook quote bypass verified cross-venue equivalence. A stale/unchanged quote is a **candidate-sourcing clue**—never proof of executable liquidity or a tradeable price. Revalidate market state and native IDs.

## 2. Initial opportunity profile: pre-match and hedgeable

Start with **one selected pre-match fixture and one resting quote**. Initial engineering and model testing remain shadow/read-only; the **first genuine fill-evidence phase is a separately authorised micro-live trial**, not paper claims about whether passive quotes get matched. No in-play offers. Prefer less volatile windows with sufficient *Polymarket* hedge depth and reliable approved regulation-time 1X2 settlement.

Major competitions may offer deeper hedges, but **do not exclude lower leagues from candidate discovery**: their thin Matchbook markets could be our gap, provided the price is stable enough, native identity is confirmed, and the hedging venue has genuinely adequate liquidity.

Distinguish deliberately:

- **Thin / inactive Matchbook quoting:** potentially valuable room to provide liquidity.
- **Thin Polymarket executable depth:** a hedge risk; cap size or decline.
- **Unmatched canonical market:** a mapping/availability investigation until verified, not a trading shortcut.
- **Stale Matchbook data:** a possible signal of neglected odds, but must be refreshed before quoting.

Example *initial trial parameters* (not permanent tenet-imposed defaults or proven profitable settings): quote only 6–24 hours pre-kickoff; stop new offers 90 minutes before kickoff; aim for 2% **conditional** net margin; propose an initial **£5 Matchbook lay stake** if the venue's current minimum stake, odds increment and account rules permit it; cap Matchbook **lay liability** independently (illustratively £10 per fixture), with additional hard caps on **total native capital committed across both venues**, maximum matched-but-unhedged exposure and maximum tolerated recovery loss. One quote and one fixture only.

**Stake is not liability.** At decimal lay odds 4.00, a £5 backer stake exposes the layer to **£15 liability** before the costs and funds needed for the Polymarket BUY hedge. That hypothetical quote must be rejected under a £10 liability limit rather than silently increasing the limit. Equally, if the exchange or Polymarket hedge has a minimum size above the configured caps, **skip the trial**, report `below_venue_minimum` / `hedge_minimum_not_supported`, and request new operator approval for any changed size. Never automatically upsize real money to satisfy an API minimum. The risk budget must include commission, FX costs, hedge expenditure and recovery capacity—not merely £5.

The selection of £5 is a **proposed small initial order**, not a verified venue minimum, guaranteed match size, permission to trade, or maximum possible loss.

## 3. Single economic and settlement authority

Calculate every proposed LAY and BUY using the **existing shared settlement, fees, FX, native Treasury and economic-payoff logic**, extending it for maker quote lifecycle rather than forking the arbitrage calculator.

For Matchbook offered decimal lay odds L and backer stake S, **lay liability = (L − 1) × S**. For the intended hedge, use the exact Polymarket token(s), fresh executable BUY price levels, hedge quantity and depth.

Compute **minimum settlement-state payoff and expected quoted margin if both legs actually execute**, after Matchbook lay-side commission, Polymarket BUY cost and fees, order-role fees, FX conversion, market/settlement terms, slippage, liability and native capital locks. Know the exact market period, draw/void/postponement rules and settlement equivalence. Unknown material fee, FX, settlement, capital or hedge inputs must fail closed for new quoting.

Show these different concepts honestly:

- **Indicative/target maker margin:** what would remain *if* the quote matched and the hedge executed as modeled; not guaranteed.
- **Unmatched resting quote:** no filled lay, no hedged position, possible pending cancel/match race.
- **Confirmed matched but unhedged lay:** actual directional exposure, potentially negative economics.
- **Complete hedge:** actual fills, fees, FX and verified settlement-state minimum payoff; not a prediction of future fill.

A Matchbook offer may be a different price from published Matchbook odds; never imply its unfilled stake is **current executable taker depth**. It must not appear as qualifying Best Arb or an arbitrage paper fill (Core Tenet 18).

## 4. Continuous repricing is quote management, not automatic hedging

STREAM supplies Polymarket live public market observations; it is **not a second arb solver, independent capital authority, or licence to execute**. Quote management is a separate lifecycle:

1. Select a fixture/registered market from UNIVERSE, BACKGROUND, HOT or the Candidate Radar.
2. Wait for a **complete, correctly sequenced, fresh Polymarket order-book state** for the approved token and for sufficient Matchbook status evidence. Missing, incomplete, disconnected or unvalidated feeds block new quotes.
3. Compute the target Matchbook lay odds, backer stake, lay liability and hypothetical hedge quantity that achieve the configured net conditional margin.
4. **Shadow stage:** log the hypothetical intended quote only; do not claim an exchange accepted or matched it. **Separately authorised micro-live stage:** only after all readiness gates are met, submit one genuinely bounded Matchbook offer and capture its authoritative exchange order ID, status and acknowledgements. Shadow signals never automatically trigger a live order.
5. When Polymarket moves, coalesce high-frequency changes, recompute hedge depth and margin, and decide to **hold, amend, reduce or cancel the unmatched Matchbook offer**. Prevent churn and respect venue pacing.
6. On insufficient hedge depth, suspension, stale/disconnected feed, risk-limit breach or pre-match cutoff, request cancellation and **wait for authoritative cancellation acknowledgement**. Cancellation submission cannot erase a simultaneous fill.
7. Only a **confirmed Matchbook match**, full or partial, triggers a hedge/recovery action for the matched amount.

Updates from the Polymarket feed ordinarily change **outstanding quotes**, not buy an anticipatory hedge. Pre-hedging absent a Matchbook fill is a different exposure-generating strategy and requires separately approved controls.

## 5. Hedge trigger, exposure and adverse selection

**Default future hedge trigger: confirmed Matchbook matched quantity.** Recalculate the exact hedge quantity for actual fills (including partial fills), independently revalidate Polymarket executable book/depth and market state, and use existing fees/FX/settlement/Treasury evidence before execution. A £5 original stake does not imply £5 of hedge expenditure; derive the necessary Polymarket BUY shares, executable cost and required account balance from the full state-payoff hedge model.

**Before posting even a £5 real lay**, independently verify that the exact Polymarket equivalent market is presently open and hedgeable at the worst permitted fill size, with adequate current executable depth, venue-specific fees, FX, **spendable collateral/allowance** and a reserved loss-recovery budget. This readiness snapshot does **not** reserve future third-party liquidity or guarantee the hedge will still be available when the lay fills. Revalidate again after every confirmed full or partial Matchbook fill. If pre-quote hedge readiness is absent, do **not** post the lay.

On a fill, **exposure reduction is higher priority than maintaining the originally desired margin**. A hedge may no longer be profitable; do not wait indefinitely for the market to revert or let an opening-arb minimum-profit requirement prevent a necessary emergency risk-reducing action.

A real-enabled implementation must explicitly model and cap:

- maximum resting liability, simultaneous quotes, native Treasury available/locked by venue and capital committed;
- maximum **matched but unhedged exposure**, maximum time unhedged, and maximum tolerated recovery loss;
- missed WebSocket updates, stale or crossed books, Matchbook order ACK/fill/cancel race, partial fills and duplicate notifications;
- provider rate-limit/backoff, loss of reference liquidity, suspension/reopen, kickoff/cutoff and manual kill switch;
- idempotent hedge retries, loss-minimising fallback / manual escalation, and an audit of all outstanding matched/unmatched portions.

**Adverse selection is a primary strategy risk.** A Matchbook lay may be matched precisely because Polymarket moved against us faster than we could amend/cancel. Maintaining a *posted target price* does not guarantee maintaining the *realised margin*.

On feed failure stop new quoting and cancel unmatched offers where authorised/possible, **but do not ignore an already matched position or silently assume cancellation succeeded**.

## 6. Architecture and priority contract

- MARKET MAKER is a **distinct strategy and view within the existing trading / Arbitrage operations workspace**, not an automatic third top-level product module (Core Tenet 01 remains in force until explicitly revised).
- Reuse **one** UNIVERSE-approved fixture/market catalogue and register (03/20), provider coordination and limits (19), venue-specific fees and FX (15), capital pools (09), fill-risk principles (18), and ledger (10). Do not create second equivalence, costs, FX, or accounting truth.
- STREAM is a fast **market-data input**, not a duplicate executable arb authority. Targeted Matchbook reads share the existing provider queue, cooldown, rate budget and anti-starvation rules.
- Already filled maker exposure and ACTIVE TRADE / Price-2 work outrank speculative quote surveillance. Do not starve HOT, BACKGROUND or UNIVERSE; ensure bounded subscriptions, coalescing and cancellation on disable/shutdown.
- Report MARKET_MAKER revenue, fee, FX, liability, exposed/hedged P&L separately by **strategy dimension within the same existing ledger**. Do not reclassify maker results as guaranteed arbitrage.
- Preserve default paper/disarmed execution, venue-account and jurisdiction controls; no geographic or other access-control bypass. Merely receiving public prices does not establish trading permissions.

## 7. Staged development and evidence requirements

**Stage A — Research and read-only candidate radar (no venue writes).** Identify thin/stale Matchbook and unmatched UNIVERSE opportunities, including reason codes why each is/is not eligible. Verify Polymarket hedge market, native contract identity, current depth and account/territory execution eligibility. Show candidate counts and limitations; no bets.

**Stage B — One-fixture shadow quote and failure testing (no venue writes).** Use STREAM and exact native mapping to compute proposed quote odds, lay stake, **liability**, cancel/reprice decisions, hedge size/cost and recovery budget. Exercise fake order-status feeds, order-ack/cancel/fill races, partial fills, duplicate notifications, webhook gaps, missing PM liquidity, REST/WS outages and kill switch with deterministic tests and conservative latency scenarios. These tests prove **software behaviour**, not that a resting passive quote would have filled in a real order queue. Paper-model P&L and maker fill rates **are not an acceptance prerequisite or substitute for actual maker fills**.

**Stage C — Separate live-order readiness and explicit operator sign-off (still no venue writes).** Before any micro-live trial, verify all of:

- Live execution capability and region/account/venue permission for **both** Matchbook and Polymarket; no access-control or geolocation workaround.
- Independently tested native **Matchbook LAY** submit, status, partial-fill and cancel-ack lifecycle, with exact native order ID, order-ownership reconciliation and a reliable way to learn whether a fill occurred while cancellation was pending.
- Independently tested Polymarket BUY capability/readiness for the actual mapped hedge token, venue minimum orders, funding/collateral, allowances, current depth and fees/FX. Live PM trade permission must not be inferred from access to the public STREAM market feed.
- A **maker-specific pre-quote execution plan** and **post-fill fresh hedge/recovery plan** meeting Core Tenets 02 and 18. The current taker opening Price-2 gate does **not** itself authorise maker execution; any necessary changes to those execution contracts must be separately proposed/reviewed rather than silently bypassed.
- Hard configurable Matchbook stake, **lay liability**, combined cash-at-risk, total outstanding quotes, max unhedged amount/time and recovery-loss limits. Prove limits are enforced by the actual venue transport, not only the UI. Native Treasury balances for *both* venues must be spendable and not double counted.
- A tested independent **market-maker kill switch**, idempotent state transitions, missed-fill recovery, reconciliation, operator supervision and a clear manual escalation / loss-limiting fallback if PM becomes unavailable.
- Owner-specific written approval of **one eligible fixture, contract, timing window, order size, liability/combined-capital limits and execution arming**, after checking current venue minimums. No automatic arming merely because a draft tenet is merged.

**Stage D — Explicitly authorised micro-live trial with genuine small orders.** Start with **one £5 Matchbook lay stake if and only if allowed within the already approved liability, native Treasury, minimum-order and Polymarket hedge-size limits**; otherwise **do not place an order**. A nominal £5 quoted stake does not cap liability or total loss. No in-play, no multiple parallel orders, no automatic size increase, and no unattended execution.

Once an exchange-authoritative full or partial Matchbook match is confirmed, **immediately initiate the separately validated Polymarket BUY hedge for the matched exposure** (subject to fresh fill-time depth/readiness). Log request, acknowledgment, actual fill amount, slippage, residual exposure and recovery action. A submitted hedge is not a completed hedge; cancellation may race with a Matchbook fill. Stop new quoting if any required system is degraded. If a hedge fails or ceases to be profitable, invoke predefined bounded risk-reduction/recovery controls rather than assuming the margin can be preserved.

Learn from **real order** evidence: unmatched dwell time, matched volume, maker fill probability with an honest quoted-time denominator, adverse selection, cancel-vs-fill latency, Matchbook fill-to-Polymarket-hedge latency, realised minimum settlement payoff after all costs, failed hedges and loss frequency. Small sample sizes remain inconclusive; only expand exposure following separately reviewed real data, not paper profitability.

**Sequence:** shadow software/readiness tests → separately approved small real maker + hedge trial → review genuine fill and recovery evidence → only then consider expansion. An extended simulated maker-fill trial is optional diagnostic work, **not a mandatory gate that purports to prove real passive-order fills**.

The STREAM Phase-1 shadow implementation can proceed independently. **This documentation change does not permit live execution, change launcher flags, deploy a strategy or authorise any £5 bet today.**

## 8. Required audit, dashboard and learning metrics

A dedicated MARKET MAKER view should show **candidate source**, exact fixture and market identity, mapped native IDs, equivalence evidence, Matchbook spread/depth/age, Polymarket hedge book age/depth, projected quote odds, **backer stake**, **lay liability**, **total combined funds-at-risk** and reserved hedge/recovery capital separately, conditional margin, status and exclusion reason. For real trials display the *explicit operator approval and independent live-arming state*, venue order acknowledgment, matched/unmatched split and hedge completion separately.

Retain immutable timestamps/evidence for proposed/accepted/amended/cancelled quotes, provider acknowledgements, *confirmed* matches, partial fills, remaining exposure, Polymarket pre-hedge executable depth and actual hedge fills, fees, FX, slippage, recovery actions and settlement.

Report separately: candidate-to-eligible conversion; time quotes were active; **real vs simulated** matched stake and fill rates (with denominators); matched-to-fully-hedged conversion; fill-to-hedge latency; maximum unhedged exposure/duration; quoted vs realised net margins; fee/FX P&L; adverse-selection loss; forced-loss hedge/recovery frequency; expired/stale/unsupported/unmatched and **below minimum size** rejection counts. Shadow-model fills may never be reported as real exchange matches.

## Review checks

- [ ] Unmatched UNIVERSE fixtures can appear on radar **without bypassing exact registration/settlement matching**.
- [ ] Thin/stale Matchbook odds are a **discovery signal**, not execution evidence; fresh Polymarket hedge depth is genuinely sufficient.
- [ ] Initial pre-match only, one quote; **£5 nominal lay stake is illustrative, with independent hard caps on lay liability and total capital at risk**. Do not silently increase stake to meet provider minimums.
- [ ] All lay/payoff/margin math shares existing fees, FX, native capital and settlement authorities.
- [ ] A resting passive offer never becomes Best Arb, executable taker size, or guaranteed profit.
- [ ] Polymarket changes manage unmatched quote; **only confirmed Matchbook fills** initiate default hedging.
- [ ] Hedge/recovery handles partial fills, cancellation races, stale/disconnected feed and adverse selection; exposure is explicit.
- [ ] Provider slots, active-trade priority, Windows runtime stability and execution-disabled defaults are preserved.
- [ ] Shadow and deterministic fake-fill tests verify logic, **not actual Matchbook maker fill probability**; no mandatory performance claims from paper orders.
- [ ] The micro-live readiness gate is separately reviewed, includes region/account permissions, native lay and hedge minimums, hedge/collateral readiness, maker-specific execution contracts, risk recovery, idempotency, and an independent kill switch.
- [ ] Actual tiny real maker orders and Polymarket BUY hedges require *explicit per-trial owner approval and live arming*. No strategy execution is authorised merely by this documentation.
- [ ] Shadow, paper and real evidence cannot be mistaken for each other; all real fills and the hedged/unhedged state are exchange-acknowledged.
