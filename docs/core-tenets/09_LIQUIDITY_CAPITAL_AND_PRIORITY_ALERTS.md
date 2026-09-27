# Core Tenet 09 — Liquidity, Capital & Priority Arb Alerts

## Principle

Sports Hedge manages capital in native venue/currency pools and treats **capital availability, lock duration, executable liquidity and capital recycling as visible arbitrage economics**. Exceptional arbitrage opportunities may be escalated for explicit manual capital approval.

Discretionary PAPER placement deploys the minimum of remaining Max Event, remaining Max Opportunity, Max One-Time, genuine incremental executable liquidity, actual spendable native Treasury, and a valid complete hedge. Max Event counts capital currently locked on that canonical event. A positive lock counts for PENDING, OPEN, PARTIAL and AWAITING_MANUAL_EXTERNAL. A missing lock contributes zero. Closed or released capital contributes zero.

Hidden reserve, pool-fraction, fixture-concentration, per-trade and recommendation haircuts do not size that amount. Risk score and fill confidence may remain telemetry. They do not size or reject an otherwise qualifying paper placement. Fail-closed gates still apply: structural admission, settlement equivalence, fresh quotes, complete books, known fees, known FX, minimum net edge, a valid complete hedge, and a real native Treasury balance.

## Automated Liquidity Pools

Standing capital may be allocated within configured limits, for example:

```text
Polymarket / USD / AUTO_POOL
Matchbook / GBP / AUTO_POOL
Smarkets / GBP / AUTO_POOL
```

Native balances remain distinct. USD and GBP must never be summed directly.

Each pool must distinguish at least:

- **available cash** — genuinely free and immediately allocatable;
- **locked capital** — committed to open paper/live positions and not spendable again;
- **conditionally releasable capital** — locked capital for which a currently executable clean unwind is observable.

Conditionally releasable capital is an analytical input, not spendable cash. It becomes available only after the required closing legs actually fill. Sports Hedge must never double-count locked capital merely because the market currently appears liquid.

## Priority Arb Alerts

Exceptional arbitrage may trigger a **Priority Arb Alert** after ordinary arb checks have already passed.

Qualification should consider more than edge alone, including:

- net guaranteed edge after costs;
- expected guaranteed profit;
- executable depth on all legs;
- quote freshness/persistence;
- execution-risk score;
- fill-confidence inputs;
- capital efficiency;
- expected lock duration / time to settlement;
- current unwindability and capital-release potential;
- opportunity cost of preventing later qualifying strikes.

## Manual Override Capital

A Priority Alert may recommend one-off `MANUAL_OVERRIDE` capital above the standing automated pool.

Manual override capital:

- is tied to a specific opportunity;
- requires explicit operator action;
- does not silently become permanent automated capital;
- remains part of the Arbitrage strategy book for reporting.

## Recommended size

The deployed size is the formula above. It is not a recommendation that is then haircut.

**Executable depth** here means Core Tenet 18 **taker** liquidity: current opposing size we can consume now at a known price, after previously PAPER-consumed liquidity has been subtracted. Displayed, resting, or passive maker/lay quotes are not executable depth. Tenet 18 is the authoritative home for taker vs maker fill semantics.

If the mispriced leg has £500 executable and the hedge leg has £5,000, the validated size is constrained by the £500 leg, then by the three operator caps and by each venue's actual spendable balance. The complete hedge is scaled together.

Native venue balances stay separate. A surplus on one venue does not fund another. Locked capital cannot be spent again. Treasury availability is the spendable native balance.

The UI should expose:

- raw limiting depth;
- marginal depth / levels consumed;
- safety/risk haircut;
- maximum validated size;
- recommended size;
- limiting constraint;
- stake plan by leg;
- expected guaranteed payoff and ROI;
- capital required by venue/currency;
- free balance after the proposed fill;
- reserve remaining;
- fill-confidence / execution-risk inputs;
- expected lock duration and capital-turnover metric;
- extra capital required beyond standing pools.

## Capital efficiency and opportunity cost

A guaranteed £10 profit that locks £1,000 for several days is not economically identical to a guaranteed £10 profit that releases the same capital in minutes.

Capital allocation should therefore consider both **return on committed capital** and **expected time until that capital can be safely released**. The precise ranking model may evolve, but it must remain explainable and must not disguise estimates as guarantees.

When capital is abundant and exit liquidity is uncertain or costly, holding an already-guaranteed position to settlement can be preferable. When capital is scarce and a clean unwind is currently executable, releasing capital early may be economically superior even if the gross locked-in settlement profit is slightly larger.

## Clean unwind and capital recycling

For an open position, Sports Hedge may evaluate a paper/live **clean unwind** using current reverse-side executable quotes.

A clean unwind decision must use:

- actual current closing prices on every required leg;
- executable closing depth;
- applicable exit fees / commissions;
- FX and slippage assumptions;
- fill confidence and execution risk;
- capital actually released if all closing legs fill;
- remaining expected lock duration;
- capital scarcity / opportunity-cost signal.

Headline spread convergence is **not** by itself a close trigger.

For exchange-style markets, reverse/lay/back mechanics and liability must be represented correctly. Lay odds must never be treated as back odds. A position can be labelled safely unwindable only when the complete closing state/economics are validated.

The system may use currently executable unwind capacity to rank opportunities or estimate capital-release value, but it must not make new allocations against that capital until the unwind is completed.

## Notifications

Priority Alerts should support provider-neutral routing such as in-app, email, push or SMS, with deduplication/cooldowns to prevent market-movement spam.

## Review checks

- Are native currency pools kept separate?
- Are available, locked and conditionally releasable capital distinguished without double counting?
- Is manual override explicitly distinguishable from auto-pool capital?
- Is recommended size based on limiting Tenet-18 taker executable depth rather than headline, displayed, or passive/maker quotes?
- Is discretionary paper size the minimum of remaining Max Event, remaining Max Opportunity, Max One-Time, Tenet-18 taker depth, actual native Treasury, and a valid complete hedge?
- Does any hidden reserve, pool-fraction, concentration, per-trade cap, risk score, or recommendation haircut reduce that amount? It must not.
- Does Max Event count positive locks on the canonical event regardless of lifecycle label, and exclude closed or released capital?
- Are expected lock duration and capital opportunity cost considered where relevant?
- Does an early unwind use executable reverse-side economics after costs rather than spread convergence alone?
- Is fill confidence described as an estimate, not a guarantee?
- Does Phase 1 still stop before real execution?
