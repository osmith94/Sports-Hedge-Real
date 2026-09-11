# Core Tenet 05 — Research & Odds-Weighted Value

## Principle

Research is probabilistic football analysis. It becomes decision-relevant only when historical/modelled expectations are compared with an economically equivalent market price.

A strong football pattern is not automatically a good bet.

## Non-negotiables

- Research models remain venue-independent.
- Matchbook may be the initial reference-price feed, but it is not the model's truth.
- Compare equivalent quotes across Matchbook, Smarkets and Polymarket when valid data exists.
- Polymarket is only included when settlement semantics are strictly equivalent.
- Show best available equivalent price independently from the reference price.
- Apply known commission/fees, quote freshness and liquidity/depth.
- If no equivalent market exists, show `No comparable market` rather than manufacturing a value signal.
- Uncertainty, N, confidence, stability and data quality must remain visible.

## Core value outputs

Where applicable, expose:

```text
model_probability
reference_price / venue
best_price / venue
raw_market_implied_probability
cost_adjusted_market_probability
probability_edge_pp
expected_profit_per_unit
expected_roi
quote_age
liquidity/depth
sample_size
confidence
stability
data_quality
regime relevance
```

Suggested result states include:

```text
VALUE
NO_VALUE
INSUFFICIENT_EVIDENCE
STALE_QUOTE
INSUFFICIENT_LIQUIDITY
MISSING_COSTS
SEMANTICS_MISMATCH
```

## Research Home

The Research Home should prioritize:

1. odds-weighted Value Signals;
2. upcoming fixtures;
3. browse entry points into teams, Matchday, Scenario Lab and Trends/Market Intelligence;
4. a clearly separate link to Arbitrage.

## Review checks

- Is the signal ranked on economic value rather than SRC alone?
- Is the compared market proposition genuinely equivalent?
- Is the best available price shown without making one venue the model truth?
- Could a user distinguish model probability from market probability and from guaranteed arbitrage?
