# Core Tenet 09 — Liquidity, Capital & Priority Arb Alerts

## Principle

Sports Hedge manages capital in native venue/currency pools and may escalate exceptional arbitrage opportunities for explicit manual capital approval.

## Automated Liquidity Pools

Standing capital may be allocated within configured limits, for example:

```text
Polymarket / USD / AUTO_POOL
Matchbook / GBP / AUTO_POOL
Smarkets / GBP / AUTO_POOL
```

Native balances remain distinct. USD and GBP must never be summed directly.

## Priority Arb Alerts

Exceptional arbitrage may trigger a **Priority Arb Alert** after ordinary arb checks have already passed.

Qualification should consider more than edge alone, including:

- net guaranteed edge after costs;
- expected guaranteed profit;
- executable depth on all legs;
- quote freshness/persistence;
- execution-risk score;
- fill-confidence inputs;
- capital efficiency.

## Manual Override Capital

A Priority Alert may recommend one-off `MANUAL_OVERRIDE` capital above the standing automated pool.

Manual override capital:

- is tied to a specific opportunity;
- requires explicit operator action;
- does not silently become permanent automated capital;
- remains part of the Arbitrage strategy book for reporting.

## Recommended size

The recommendation is constrained by the **limiting executable leg**, then reduced by configured safety/risk/operator limits.

If the mispriced leg has £500 executable and the hedge leg has £5,000, the validated size is constrained by the £500 leg before any safety haircut.

The UI should expose raw limiting depth, haircut, recommended size, validated maximum, stake plan by leg, expected guaranteed payoff, fill-confidence inputs and extra capital required beyond standing pools.

## Notifications

Priority Alerts should support provider-neutral routing such as in-app, email, push or SMS, with deduplication/cooldowns to prevent market-movement spam.

## Review checks

- Are native currency pools kept separate?
- Is manual override explicitly distinguishable from auto-pool capital?
- Is recommended size based on limiting executable depth rather than headline liquidity?
- Is fill confidence described as an estimate, not a guarantee?
- Does Phase 1 still stop before real execution?
