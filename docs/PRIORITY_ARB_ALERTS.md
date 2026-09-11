# Sports Hedge — Priority Arb Alerts & Manual Capital Escalation

## Purpose

Sports Hedge has standing **Automated Liquidity Pools** that the system may allocate within configured limits in paper mode. Exceptional arbitrage opportunities should be able to escalate beyond those standing limits without silently increasing automated risk.

The product concept is **Priority Arb Alerts**.

A Priority Arb Alert is an exceptional, high-quality arbitrage candidate that has:
- passed strict canonical event / settlement equivalence checks;
- passed fee / FX / cost checks;
- meaningful net guaranteed edge after costs;
- strong executable depth on every required leg;
- fresh quotes;
- acceptable execution risk;
- sufficient expected profit / capital efficiency to justify operator attention.

It is not a separate arb solver. It is an escalation layer on top of the existing arbitrage engine.

## Capital concepts

### Automated Liquidity Pool
Standing capital authorised for routine automated/paper allocation.

Examples:
```text
Polymarket / USD / AUTO_POOL = $100
Matchbook / GBP / AUTO_POOL = £100
Smarkets / GBP / AUTO_POOL = £100
```

### Manual Override Capital
One-off additional capital explicitly approved by the operator for a specific opportunity.

```text
capital_source = MANUAL_OVERRIDE
```

Manual override capital must never silently become part of the permanent automated pool.

## Priority alert qualification

A normal arbitrage opportunity may be executable without being a Priority Alert.

Priority qualification should use configurable thresholds such as:
```text
minimum_net_edge
minimum_expected_profit
minimum_executable_depth
maximum_quote_age
maximum_execution_risk
minimum_depth_coverage
minimum_fill_confidence
minimum_capital_efficiency
```

A Priority Alert should only fire after the ordinary arb checks have already confirmed economic equivalence and positive minimum payoff across valid settlement states.

## Recommended manual size

The recommendation must be constrained by the **limiting executable leg**, not by the largest visible pool or the most attractive quote.

Conceptually:
```text
recommended_size = min(
    executable_depth_all_required_legs_after_safety_haircut,
    operator_manual_cap,
    venue_limit,
    risk_limit
)
```

For example:
```text
Smarkets mispriced leg executable depth: £500
Hedge side executable depth: £5,000
Safety haircut: 5%

Raw limiting depth: £500
Recommended manual size: approximately £475
```

The exact stake allocation across legs should still come from the existing settlement-state / hedge solver.

The UI must expose:
- raw visible depth;
- safety haircut;
- recommended size;
- maximum theoretically executable size;
- expected guaranteed profit at recommended size;
- expected guaranteed ROI;
- capital required by currency / venue;
- limiting leg;
- quote age;
- execution-risk score.

## Fill confidence

`fill_confidence` is an operational estimate, not a guarantee.

Possible inputs:
- depth coverage ratio;
- quote freshness;
- quote persistence;
- venue reliability / observed cancellation rate when data exists;
- number of book levels required;
- latency assumptions;
- historical paper fill performance.

The underlying inputs must remain visible.

## Alert severity

Suggested levels:
```text
PRIORITY
HIGH_PRIORITY
CRITICAL
```

Severity should reflect economics and execution quality, not edge alone.

Example:
```text
CRITICAL
Net guaranteed edge: 9.8%
Recommended capital: £475
Expected guaranteed profit: £46.55
Limiting leg depth: £500
Fill confidence: HIGH
Quote age: 180ms
```

## Notification channels

Provider-neutral notification contract should support future channels such as:
```text
IN_APP
EMAIL
PUSH
SMS
```

Phase 1 may implement in-app plus an email adapter seam / test provider.

Alerts must be deduplicated and rate-limited so one moving market does not spam the operator repeatedly.

Useful events:
```text
PRIORITY_ALERT_OPENED
PRIORITY_ALERT_UPGRADED
PRIORITY_ALERT_DOWNGRADED
PRIORITY_ALERT_EXPIRED
MANUAL_OVERRIDE_PREPARED
MANUAL_OVERRIDE_CANCELLED
```

## Operator workflow

```text
1. Priority alert fires
2. Operator opens opportunity
3. Sports Hedge shows all legs and limiting depth
4. Recommended size is prefilled
5. Operator may reduce / increase within validated maximum
6. UI recalculates hedge stakes, guaranteed payoff and capital by venue/currency
7. Operator prepares a MANUAL_OVERRIDE ticket
8. Phase 1 stops before any real venue order placement
```

Any later real-execution integration must be a separate explicit safety/architecture phase.

## Accounting dimensions

Relevant opportunity / position / journal records should retain:
```text
strategy_book = ARBITRAGE
capital_source = AUTO_POOL | MANUAL_OVERRIDE
priority_alert_id
opportunity_id
venue
currency
```

This allows reporting of:
- automated arbitrage P&L;
- manually escalated arbitrage P&L;
- capital used from standing pools;
- capital used from one-off overrides.

One audited ledger remains the source of truth.

## UI

The Arbitrage Operations Dashboard should have a visually distinct **Priority Alerts** area above or alongside the Near-Arb Watchlist.

A Priority Alert card should answer immediately:
- Why is this exceptional?
- How much can actually be filled?
- What is the recommended size?
- Which leg is limiting?
- What is the guaranteed return after costs?
- How much additional capital is required beyond the automated pools?

## Safety / current phase

Phase 1 remains PAPER MODE / read-only toward venues.

No real order placement, cancellation, wallet signing or trading credentials are introduced by this feature.
