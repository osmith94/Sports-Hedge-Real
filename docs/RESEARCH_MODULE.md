# Sports Hedge — Research Module

## Product structure

Sports Hedge has two distinct top-level modules:

1. **Arbitrage** — identifies economically equivalent cross-venue discrepancies and paper-executes/reconciles them.
2. **Research** — football analysis, team/fixture browsing, Scenario Response Profiles, manager/regime context, market intelligence, trends and odds-weighted value signals.

Research signals are probabilistic and can lose. They must never be presented as arbitrage or guaranteed profit.

## Research Home

The Research Home is the first snapshot page for analysis.

It should show:

- **Value Signals** — strongest current odds-weighted analytical opportunities.
- **Upcoming Fixtures** — near-term Premier League, Championship, La Liga and Champions League-ready matches.
- **Browse** — Teams, Fixtures/Matchday, Scenario Lab, Trends/Market Intelligence.
- **Arbitrage Workspace** — a clearly separate entry into the arbitrage module.

A pre-match flow should support:

```text
Research Home
  -> Upcoming fixture
  -> Team profile
  -> Scenario Response Profile
  -> Scenario Lab
  -> Paper scenario/watch rule
```

while Arbitrage remains a separate workflow.

## Scenario strength is not value

A high Scenario Response Coefficient (SRC) does not by itself imply a bet is attractive.

For a market-relevant scenario, Sports Hedge should compare the model against an economically equivalent current market proposition.

Core outputs:

```text
model_probability
reference_price
reference_venue
best_price
best_venue
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
manager/regime relevance
```

If no equivalent market exists, show `No comparable market` rather than fabricating one.

## Venue policy

The Research model is **venue-independent**.

- Matchbook may be used as the initial **reference price feed** when available.
- Smarkets should be included when authorised data/access is available.
- Polymarket should only be included where the proposition is strictly economically equivalent under the canonical settlement fingerprint.
- The UI should show the **best available equivalent price** independently from the reference quote.

No venue is the source of truth for the football model itself.

## Value Signal

A Research Value Signal combines:

```text
historical/team scenario response
+
manager/regime context
+
model probability or distribution
+
current equivalent market price
+
fees/commission
+
quote freshness
+
liquidity/depth
+
uncertainty/sample quality
```

The value ranking should therefore not simply sort by SRC.

Suggested statuses:

```text
VALUE
NO_VALUE
INSUFFICIENT_EVIDENCE
STALE_QUOTE
INSUFFICIENT_LIQUIDITY
MISSING_COSTS
SEMANTICS_MISMATCH
```

## Team pages

Each team page should support Football-Manager-style browsing and include:

- next fixture;
- current manager/regime;
- strongest and weakest scenario responses;
- current-manager vs prior-manager splits;
- odds/value overlay for market-relevant scenarios;
- sample size, confidence and data quality;
- links to Scenario Lab and Matchday.

## Matchday

The Matchday page should help answer:

> What should I inspect before and during this fixture?

For each fixture, show team/regime context plus the most relevant odds-weighted scenarios, then allow drill-down into teams/scenarios or separately into the Arbitrage workspace.

## Separation from Arbitrage

Research value is directional/probabilistic.

Arbitrage is a settlement-state guaranteed-profit test after costs.

They may share canonical event/market identities and venue adapters, but their analytics, terminology and user flows must remain distinct.
