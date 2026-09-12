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
- sufficient expected profit / capital efficiency to justify operator attention;
- sufficient estimated **opportunity survivability** for the expected action/confirmation latency.

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

### Manual External / external-operator capital
A generic `MANUAL_EXTERNAL` source for a required leg that Sports Hedge itself cannot execute.

```text
capital_source = MANUAL_EXTERNAL
execution_mode = EXTERNAL_OPERATOR
lifecycle_state = AWAITING_EXTERNAL_LEG_CONFIRMATION
operator_action = PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY
```

Rules:
- that leg must not draw from `AUTO_POOL`;
- there is a hard stop before any automated counterpart leg is committed;
- the operator action is never `PLACE BET`;
- confirmation records venue/product, operator/counterparty reference, executed price, size, currency, timestamp, and optional evidence;
- remaining hedge legs are revalidated against a **fresh** quote/cost snapshot with the confirmed external stake treated as **fixed realised exposure** (not a max-stake the solver may shrink). If remaining depth cannot hedge the full confirmed amount while keeping a positive minimum net payoff after costs/FX, fail closed;
- beneficial-owner / jurisdiction / account eligibility stay outside the execution engine. The engine only accepts an explicit `eligibility_confirmed` flag;
- `MANUAL_EXTERNAL` is a distinct accounting source from `AUTO_POOL` and ordinary `MANUAL_OVERRIDE`.

Phase 1 remains paper-only: confirmation and revalidation are audit/state contracts. They do not place, cancel, or commit venue orders.

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
minimum_survivability_score
minimum_survival_probability_at_required_latency
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
- execution-risk score;
- survivability score / horizon;
- volatility regime and the main survivability drivers.

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

## Opportunity survivability

`survivability` estimates the chance that an opportunity remains economically valid long enough for the required action to complete.

It is related to, but distinct from, fill confidence. Fill confidence asks whether the displayed size is likely to fill; survivability asks whether the cross-venue edge is likely to still exist by the time the workflow reaches the point of commitment.

Inputs should include where available:

```text
recent realised price volatility on all required legs
rate/direction of recent price movement
cross-venue convergence or divergence speed
quote persistence and age
spread and executable depth
rate of depth cancellation / replenishment
historical duration / half-life of comparable dislocations
venue suspension/reopen behaviour
current event-driven repricing state
expected automated action latency
expected MANUAL_EXTERNAL counterparty confirmation latency
```

A calm, persistent 1% edge should normally score as more survivable than the same 1% edge during rapid repricing.

These are estimates, not guarantees. Phase 1 carries an optional typed seam (`OpportunitySurvivability`) on the Priority Alert read model so a later historical scorer can plug in without another contract fork. Missing or unmodelled values remain explicit/null; this layer must not invent probabilities.

Suggested outputs:

```text
survivability_score: 0-100 or null
survival_probability_at_required_latency
required_action_latency_seconds
survivability_confidence
survival_probability_5s
survival_probability_15s
survival_probability_30s
survival_probability_60s
estimated_median_remaining_life
historical_dislocation_half_life
volatility_regime
recent_volatility
survivability_reasons / component drivers
```

Where historical data is weak, the UI must state that uncertainty rather than invent precise probabilities. Survivability remains distinct from current arb edge and fill confidence.

For `MANUAL_EXTERNAL`, expected external confirmation latency is the relevant action horizon. The operator view should compare that latency to survivability. Example:

```text
Expected external confirmation latency: 30s
Estimated P(opportunity still valid at 30s): 18%
Assessment: LOW SURVIVABILITY
```

This should reduce alert priority or show an explicit warning even when the current edge is attractive.

## Alert severity

Suggested levels:
```text
PRIORITY
HIGH_PRIORITY
CRITICAL
```

Severity should reflect economics, execution quality and survivability, not edge alone.

Example:
```text
CRITICAL
Net guaranteed edge: 9.8%
Recommended capital: £475
Expected guaranteed profit: £46.55
Limiting leg depth: £500
Fill confidence: HIGH
Survivability at expected action latency: 84%
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
MANUAL_EXTERNAL_AWAITING_CONFIRMATION
MANUAL_EXTERNAL_CONFIRMED
HEDGE_REVALIDATION_FAILED
```

## Operator workflow

Standard manual-override workflow:

```text
1. Priority alert fires
2. Operator opens opportunity
3. Sports Hedge shows all legs, limiting depth and survivability
4. Recommended size is prefilled
5. Operator may reduce / increase within validated maximum
6. UI recalculates hedge stakes, guaranteed payoff and capital by venue/currency
7. Operator prepares a MANUAL_OVERRIDE ticket
8. Phase 1 stops before any real venue order placement
```

External-manual workflow:

```text
1. Priority alert fires with MANUAL_EXTERNAL requirement
2. Sports Hedge shows required external size/price and survivability versus expected response latency
3. Opportunity enters AWAITING_EXTERNAL_LEG_CONFIRMATION
4. No automated counterpart leg is committed
5. External execution details are recorded exactly
6. Sports Hedge obtains a fresh hedge snapshot and revalidates the full fixed external exposure
7. If positive minimum payoff no longer survives, fail closed
8. Phase 1 remains paper-only for Sports Hedge execution
```

Any later real-execution integration must be a separate explicit safety/architecture phase.

## Accounting dimensions

Relevant opportunity / position / journal records should retain:
```text
strategy_book = ARBITRAGE
capital_source = AUTO_POOL | MANUAL_OVERRIDE | MANUAL_EXTERNAL
priority_alert_id
opportunity_id
venue
currency
```

This allows reporting of:
- automated arbitrage P&L;
- manually escalated arbitrage P&L;
- externally confirmed manual-leg economics;
- capital used from standing pools;
- capital used from one-off overrides;
- capital used from external-operator counterparties.

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
- How likely is this opportunity to survive long enough to act?
- What current volatility / market behaviour is driving that estimate?

## Safety / current phase

Phase 1 remains PAPER MODE / read-only toward venues.

No real order placement, cancellation, wallet signing or trading credentials are introduced by this feature.
