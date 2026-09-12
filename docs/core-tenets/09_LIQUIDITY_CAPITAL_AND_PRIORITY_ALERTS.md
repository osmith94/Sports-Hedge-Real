# Core Tenet 09 — Liquidity, Capital & Priority Arb Alerts

## Principle

Sports Hedge manages capital in native venue/currency pools and treats **capital availability, lock duration, executable liquidity and capital recycling as first-class arbitrage economics**. Exceptional arbitrage opportunities may be escalated for explicit manual capital approval, but no opportunity should monopolise scarce capital merely because its headline edge or displayed depth is large.

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

The recommendation is constrained by the **limiting executable leg**, then reduced by configured safety, liquidity, risk, reserve and operator limits.

If the mispriced leg has £500 executable and the hedge leg has £5,000, the validated size is constrained by the £500 leg before any safety haircut.

Available venue balance is also a hard bound, but the system should not automatically deploy the entire remaining pool when depth permits it. Sizing must preserve configured reserves and account for concentration, execution risk, quote survivability, manual-external latency and other simultaneous or likely opportunities.

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
- Is recommended size based on limiting executable depth rather than headline liquidity?
- Does sizing preserve reserve/concurrency constraints rather than blindly consuming the full pool?
- Are expected lock duration and capital opportunity cost considered where relevant?
- Does an early unwind use executable reverse-side economics after costs rather than spread convergence alone?
- Is fill confidence described as an estimate, not a guarantee?
- Does Phase 1 still stop before real execution?
