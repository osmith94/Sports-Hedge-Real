# Core Tenet 21 — Conservative Market Making and Cross-Venue Hedging

## Principle

Sports Hedge may develop a **separate MARKET MAKER strategy** that posts resting **LAY** liquidity on Matchbook and monitors an equivalent Polymarket hedge through STREAM. This is an experiment in *providing missing Matchbook liquidity*, not simply finding pre-existing two-sided taker arbitrage.

> **Discover gaps through UNIVERSE. Observe through STREAM. Quote conservatively. Reprice or withdraw unfilled Matchbook offers when the reference moves. Hedge confirmed Matchbook fills promptly. Never call an unmatched maker offer a guaranteed arb.**

A target quoted margin is conditional on both the lay being matched and the hedge executing. It is **not a guaranteed profit** while an offer rests. This tenet authorises a future design and research strategy, **not venue order placement** or turning on real execution.

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

Start with **one selected pre-match fixture and one resting quote**, paper/shadow only; no in-play offers. Prefer less volatile windows with sufficient *Polymarket* hedge depth and reliable approved regulation-time 1X2 settlement.

Major competitions may offer deeper hedges, but **do not exclude lower leagues from candidate discovery**: their thin Matchbook markets could be our gap, provided the price is stable enough, native identity is confirmed, and the hedging venue has genuinely adequate liquidity.

Distinguish deliberately:

- **Thin / inactive Matchbook quoting:** potentially valuable room to provide liquidity.
- **Thin Polymarket executable depth:** a hedge risk; cap size or decline.
- **Unmatched canonical market:** a mapping/availability investigation until verified, not a trading shortcut.
- **Stale Matchbook data:** a possible signal of neglected odds, but must be refreshed before quoting.

Example *initial test parameters* (not permanent tenet-imposed defaults or proven profitable settings): quote only 6–24 hours pre-kickoff; stop new offers 90 minutes before kickoff; aim for 2% conditional net margin; cap Matchbook lay **liability** at £10/fixture. Operator risk settings, venue minimums, and empirical tests can require tighter rules.

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
4. **Shadow stage:** log the hypothetical intended quote only; do not claim an exchange accepted or matched it.
5. When Polymarket moves, coalesce high-frequency changes, recompute hedge depth and margin, and decide to **hold, amend, reduce or cancel the unmatched Matchbook offer**. Prevent churn and respect venue pacing.
6. On insufficient hedge depth, suspension, stale/disconnected feed, risk-limit breach or pre-match cutoff, request cancellation and **wait for authoritative cancellation acknowledgement**. Cancellation submission cannot erase a simultaneous fill.
7. Only a **confirmed Matchbook match**, full or partial, triggers a hedge/recovery action for the matched amount.

Updates from the Polymarket feed ordinarily change **outstanding quotes**, not buy an anticipatory hedge. Pre-hedging absent a Matchbook fill is a different exposure-generating strategy and requires separately approved controls.

## 5. Hedge trigger, exposure and adverse selection

**Default future hedge trigger: confirmed Matchbook matched quantity.** Recalculate the exact hedge quantity for actual fills (including partial fills), independently revalidate Polymarket executable book/depth and market state, and use existing fees/FX/settlement/Treasury evidence before execution.

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

**Stage A — Research and read-only candidate radar.** Identify thin/stale Matchbook and unmatched UNIVERSE opportunities, including reason codes why each is/is not eligible. Verify Polymarket hedge market and depth. Show candidate counts and limitations; no bets.

**Stage B — One-fixture shadow quote.** Use STREAM and exact native market mapping to compute desired quote, cancellation/repricing decisions and intended hedge size. Model conservative order-queue and latency outcomes from observed feeds; label **hypothetical match probability**, not an observed Matchbook fill. No venue write.

**Stage C — Controlled paper lifecycle.** Simulate resting quotes, partial and adverse-selection fills, cancellation races, prompt hedge revalidation, residual exposure/recovery and Treasury constraints. Do not fabricate paper fills from passive offers merely being posted.

**Stage D — Explicitly authorised micro-live trial only.** Separately reviewed execution permissions, order-status and reconciliation readiness, native kill switch, hard caps, validated shadow/paper evidence and manual sign-off. Scale only on measured **real matched-to-hedged** outcomes (Core Tenet 18).

The STREAM Phase-1 shadow implementation can proceed independently. This document is **not approval** to add MARKET MAKER to that PR, execute real orders or deploy anything.

## 8. Required audit, dashboard and learning metrics

A dedicated MARKET MAKER view should show **candidate source**, exact fixture and market identity, mapped native IDs, equivalence evidence, Matchbook spread/depth/age, Polymarket hedge book age/depth, projected quote odds, stake, **liability**, conditional margin, native capital requirement, status and exclusion reason.

Retain immutable timestamps/evidence for proposed/accepted/amended/cancelled quotes, provider acknowledgements, *confirmed* matches, partial fills, remaining exposure, Polymarket pre-hedge executable depth and actual hedge fills, fees, FX, slippage, recovery actions and settlement.

Report separately: candidate-to-eligible conversion; time quotes were active; matched stake and **fill rate** (with denominator); matched-to-fully-hedged conversion; fill-to-hedge latency; maximum unhedged exposure/duration; quoted vs realised net margins; fee/FX P&L; adverse-selection loss; forced-loss hedge/recovery frequency; stale/unsupported/unmatched rejection counts.

## Review checks

- [ ] Unmatched UNIVERSE fixtures can appear on radar **without bypassing exact registration/settlement matching**.
- [ ] Thin/stale Matchbook odds are a **discovery signal**, not execution evidence; fresh Polymarket hedge depth is genuinely sufficient.
- [ ] One initial pre-match quote and strictly bounded size/liability; no initial in-play trading.
- [ ] All lay/payoff/margin math shares existing fees, FX, native capital and settlement authorities.
- [ ] A resting passive offer never becomes Best Arb, executable taker size, or guaranteed profit.
- [ ] Polymarket changes manage unmatched quote; **only confirmed Matchbook fills** initiate default hedging.
- [ ] Hedge/recovery handles partial fills, cancellation races, stale/disconnected feed and adverse selection; exposure is explicit.
- [ ] Provider slots, active-trade priority, Windows runtime stability and execution-disabled defaults are preserved.
- [ ] Shadow, paper and real evidence cannot be mistaken for each other; no real execution without separate approval.
