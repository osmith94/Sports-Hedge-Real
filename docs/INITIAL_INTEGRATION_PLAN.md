# Sports Hedge — Initial Integration Plan

**Status:** Phase 1 implementation baseline  
**Date:** 11 September 2026  
**Mode:** Read-only market data + paper trading only  
**Live execution:** Out of scope for Phase 1

## Objective

Build a paper-trading football arbitrage dashboard that ingests real market data, matches equivalent markets across venues, models executable liquidity and costs, and simulates trades without placing any real wager.

The Phase 1 question is simple:

> Does enough real, executable arbitrage exist — at meaningful size and frequency — to justify building live execution?

## Initial venues

### Matchbook

Primary official API integration.

Phase 1 reads:

- football events;
- markets;
- runners/outcomes;
- prices;
- order-book depth / available liquidity;
- venue metadata needed for paper simulation.

The Phase 1 codebase contains no Matchbook order-placement interface.

### Polymarket

Public/read-only research integration only.

Phase 1 may use permitted public Gamma/CLOB data for:

- event and market discovery;
- token/outcome metadata;
- prices;
- order-book depth;
- spread and liquidity analysis;
- cross-venue paper arbitrage testing.

Capabilities are fixed to:

```text
data_enabled = true
paper_enabled = true
execution_enabled = false
```

No wallet, signing, authenticated trading, VPN/proxy bypass, or geolocation-circumvention functionality belongs in Phase 1.

### Smarkets

Deferred venue. Preserve an adapter slot, but do not pay for API access until the paper system demonstrates that adding Smarkets is economically justified.

## Architectural rule

Detection, simulation and execution are separate layers.

Phase 1 implements only:

```text
Market Data
    ↓
Normalization / Matching
    ↓
Arbitrage Detection
    ↓
Paper Simulation
    ↓
Analysis / Dashboard
```

There is no execution layer.

## Canonical venue interface

Venue implementations must expose market-data methods only:

- list events;
- list markets;
- retrieve order-book data;
- health/status;
- capability flags.

There must be no `place_order`, `cancel_order`, wallet-signing, or equivalent execution method in the Phase 1 interface.

## Core data model

The internal model must normalize:

- venue;
- event ID;
- sport;
- competition;
- home/away participants;
- kickoff UTC;
- market family;
- period;
- line/handicap;
- outcome/runner;
- prices;
- available size;
- settlement rules;
- raw venue payload.

## Market matching

Cross-venue candidates must be validated on more than name similarity.

Matching should include:

1. sport;
2. competition;
3. normalized team names;
4. kickoff time;
5. market family;
6. period;
7. handicap/line;
8. settlement treatment;
9. extra-time / penalties handling;
10. void/postponement rules.

Ambiguous mappings are rejected from realistic paper P&L.

## Arbitrage engine

Design for:

- two-way arbitrage;
- football 1/X/2 three-way arbitrage;
- mixed-venue combinations;
- generalized multi-leg payoff-state solving.

A candidate counts as arbitrage only when the minimum net payoff across every valid settlement state is positive after modeled costs.

## Liquidity-aware paper trading

Paper mode must not assume infinite liquidity at the displayed price.

Realistic simulation should model:

- book depth;
- weighted fill price;
- price impact;
- spread;
- slippage;
- partial fills;
- latency between legs;
- stale quotes;
- fees;
- GBP/USD FX;
- exit/unwind costs;
- capital lock time.

Keep an ideal mode only for validating the mathematics.

## Dynamic stake sizing

Stake size is derived from:

- available depth on every leg;
- bankroll by venue/currency;
- marginal slippage;
- exposure limits;
- minimum net edge;
- execution-risk estimate;
- capital efficiency.

The optimizer may deliberately use less than the maximum available liquidity when additional size destroys the edge.

## Capital velocity

Rank opportunities on more than headline ROI.

Track:

- net GBP profit;
- capital employed;
- expected settlement/lock time;
- return on capital;
- return per capital-hour/day.

A smaller, faster-returning arbitrage can be superior to a larger trade that locks capital for days.

## Functional currency

GBP is the functional and presentation currency.

For USD/stablecoin-style markets track:

- native stake/value;
- GBP equivalent;
- spot FX;
- conversion spread;
- conversion fees;
- realized/unrealized FX impact.

## Dashboard MVP

Desktop-first trading-terminal style.

Show:

- PAPER MODE status;
- venue health;
- paper bankroll;
- available/deployed capital;
- opportunities;
- gross edge;
- net edge;
- executable size;
- proposed paper stake;
- guaranteed modeled profit;
- execution-risk score;
- mapping confidence;
- capital velocity;
- paper P&L;
- rejected opportunities and reason.

## Initial development sequence

1. Repository + backend skeleton + CI.
2. Matchbook read-only API integration.
3. Polymarket public/read-only data integration.
4. Canonical football market normalization.
5. Cross-venue market matching and settlement fingerprints.
6. Two-way and three-way arbitrage solver.
7. Liquidity-aware paper execution engine.
8. GBP/FX and fee model.
9. Dashboard.
10. Observation period and economics review.

## Gate to any future live-execution phase

Do not build automated execution until paper results demonstrate:

- reliable mappings;
- realistic fill modelling;
- sufficient opportunity frequency;
- meaningful executable size;
- positive net paper P&L after costs;
- acceptable execution risk;
- stable data ingestion;
- complete auditability.

## Phase 1 definition of done

Phase 1 is complete when Sports Hedge can ingest Matchbook and permitted public Polymarket football data, safely normalize and match markets, model order-book liquidity and costs, find multi-leg arbitrage, simulate realistic paper fills, calculate GBP P&L/capital velocity, and present the results in a professional dashboard — with no real betting capability anywhere in the application.
