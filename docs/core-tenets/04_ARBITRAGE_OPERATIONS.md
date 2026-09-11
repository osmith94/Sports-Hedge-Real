# Core Tenet 04 — Arbitrage Operations

## Principle

The Arbitrage module is an operational system for finding, monitoring, simulating and reconciling economically valid cross-venue arbitrage.

It must model what can actually be filled, not just what looks profitable at top of book.

## Non-negotiables

- Model executable order-book depth, weighted price, slippage, partial fills, latency and quote staleness.
- Model known venue fees/commission and FX costs before calling an opportunity profitable.
- Missing material costs or FX should fail closed; never silently assume 1:1 currency conversion.
- Dynamic sizing may deliberately use less than visible maximum depth when more size destroys edge.
- Capital lock time and capital velocity matter alongside headline ROI.
- Keep ideal/theoretical mode only as a math/reference mode; realistic paper mode is the operational benchmark.

## Near-Arb Watchlist

The dashboard should retain the best non-executable candidates that are close to a configured trigger.

Example:

```text
minimum net trigger = 1.00%
current candidate = 0.80%
distance to trigger = 0.20pp
status = APPROACHING
```

A below-threshold candidate is not yet arbitrage and must be labelled accordingly.

## Lifecycle visibility

The system should expose an append-only operational history such as:

```text
WATCHING
APPROACHING
TRIGGERED
PAPER_FILLING
PARTIAL
FILLED
CLOSED
EXPIRED
REJECTED
```

The dashboard should answer at a glance:

- what is close to triggering?
- what has triggered?
- what filled or failed?
- what capital is available/locked?
- what has the Arbitrage strategy made?

## Review checks

- Is the displayed edge net of known costs?
- Is limiting depth visible and used for sizing?
- Are stale/partial/rejected states preserved rather than hidden?
- Are near opportunities clearly distinguished from executable arbs?
- Can the operator trace an opportunity through its lifecycle?
