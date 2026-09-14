# Sports Hedge — Core Architecture

## Status

Phase 1 architecture for a paper-only football arbitrage research platform.

The system is deliberately a **modular monolith** at this stage: one backend deployable with strong internal module boundaries. This keeps iteration fast while preserving clean seams for later extraction if data volume or execution requirements justify it.

## Architectural goals

Sports Hedge must:

- ingest market data from multiple venues without coupling the core to any one venue;
- normalize venue-specific football events and markets into canonical domain objects;
- only compare markets with equivalent settlement semantics;
- detect two-way, three-way and generalized multi-leg arbitrage;
- size opportunities from executable order-book depth rather than top-of-book headlines;
- model fees, FX, slippage, partial fills and capital lock-up;
- simulate all execution in Phase 1;
- make every decision reproducible from an audit snapshot;
- remain incapable of live order placement during Phase 1.

## High-level data flow

```text
Venue APIs
   |
   v
[venue adapters]
   |
   v
[raw market snapshots]
   |
   v
[normalization]
   |
   v
[canonical football event + market graph]
   |
   v
[cross-venue matching]
   |
   v
[fee + FX enrichment]
   |
   v
[arbitrage solver]
   |
   v
[liquidity-aware stake optimiser]
   |
   v
[execution-risk scoring]
   |
   v
[paper execution simulator]
   |
   v
[positions / P&L / capital velocity]
   |
   v
[API + dashboard]
```

## Module boundaries

### `venues/`

Purpose: external market-data access only.

Responsibilities:

- authentication where required;
- rate-limit aware HTTP/WebSocket access;
- raw event/market/order-book retrieval;
- venue health and capability reporting;
- preserving source payloads.

Rules:

- venue modules must not contain arbitrage logic;
- venue modules must not decide whether two markets are equivalent;
- Phase 1 venue contracts expose no order-placement methods.

Initial adapters:

- Matchbook — authenticated official API;
- Polymarket — public read-only Gamma/CLOB data;
- Smarkets — reserved for later.

### `domain/`

Purpose: stable venue-independent language of the system.

Contains:

- canonical football event identity;
- market family and period enums;
- canonical outcomes;
- settlement fingerprints;
- order-book levels;
- venue capability metadata;
- opportunity and position value objects.

Domain models must not import venue clients, HTTP libraries, FastAPI, or persistence implementations.

### `normalization/`

Purpose: translate venue-specific labels into canonical football concepts.

Responsibilities:

- normalize team and competition names;
- maintain curated alias maps;
- infer canonical market families;
- normalize periods and goal/handicap lines;
- construct settlement fingerprints;
- emit confidence and rejection reasons.

Normalization may propose a mapping, but ambiguous transformations must remain explicit rather than silently guessed.

### `matching/`

Purpose: determine whether canonical objects from different venues represent the same economic event/market.

Event matching considers:

- normalized home/away teams;
- kickoff-time tolerance;
- competition consistency.

Market matching requires:

- matched events;
- equal canonical market family;
- equal period;
- equal line/handicap where applicable;
- equal settlement fingerprint;
- compatible outcome state space.

A market must be rejected if settlement equivalence cannot be established.

### `arbitrage/`

Purpose: solve the economics of a matched market set.

The solver operates on a **state/payoff representation**, not venue names.

For simple complete outcome sets (2-way and 3-way), the engine can use reciprocal-price arithmetic. The public interface is intentionally broader so later back/lay and synthetic combinations can be represented as payoff vectors across settlement states.

Output includes:

- gross edge;
- fees and FX costs;
- minimum payoff across states;
- total capital required;
- candidate stakes;
- maximum executable size constrained by depth;
- capital-efficiency metrics.

`arbitrage/watchlist/` is a paper-only read model for near opportunities approaching the configured trigger. Below-threshold items are watch candidates, not arbitrage. `TRIGGERED` is reserved for candidates that already pass the existing settlement, cost, depth and risk gates. Lifecycle history is append-only.

Live collection is currently a **single** `LiveRefreshCoordinator` cadence (default 30s) whose Tracked board is the latest completed collection cohort. Issue #158 proposes one scheduler with HOT (30s) and UNIVERSE (180s) lanes and a current-state Tracked merge; see `docs/DUAL_CADENCE_SCANNER.md` and `docs/adr/0002-dual-cadence-scanner.md`. That change is design-only until architect review and must not race the #118 / #157 scan-timeout correction.

### `liquidity/`

Purpose: convert quoted prices into executable prices for a requested stake.

Responsibilities:

- walk order-book levels;
- calculate weighted average price;
- calculate worst fill price;
- calculate consumed depth;
- reject impossible stake sizes;
- expose marginal edge deterioration by stake size.

No arbitrage candidate is executable merely because top-of-book prices form an arbitrage.

### `fees/`

Purpose: venue-specific economic cost models behind a common interface.

Responsibilities:

- exchange commission;
- maker/taker fees where applicable;
- withdrawal/network assumptions where relevant;
- configurable fee snapshots at decision time.

### `treasury/`

Purpose: bankroll and currency constraints.

Responsibilities:

- GBP functional currency;
- native venue balances;
- GBP/USD conversion;
- FX spread and conversion cost;
- capital availability by venue;
- committed vs available capital;
- capital lock duration.

### `risk/`

Purpose: quantify execution risk before paper simulation.

Inputs include:

- depth;
- spread;
- size/depth ratio;
- quote age;
- recent volatility;
- number of legs;
- time to kickoff;
- latency assumption;
- hedge liquidity.

Outputs include:

- 0–100 execution-risk score;
- recommended maximum stake;
- required edge buffer;
- structured rejection reasons.

### `paper/`

Purpose: deterministic and realistic simulated execution.

Two modes:

- ideal — validates mathematics;
- realistic — models depth, latency, slippage, partial fills and stale quotes.

Phase 1 contains no live execution implementation.

### `application/`

Purpose: orchestration/use cases.

Examples:

- refresh venue snapshots;
- normalize markets;
- build matched market sets;
- scan for arbitrage;
- simulate an opportunity;
- calculate paper portfolio/P&L;
- expose dashboard read models.

Application services depend on domain interfaces and modules, not HTTP payload structure.

### `persistence/`

Purpose: durable audit and replay.

The intended production store is PostgreSQL. Phase 1 can begin with repository interfaces and lightweight implementations before schema migration tooling is introduced.

Persist:

- raw snapshots;
- canonical entities;
- mapping decisions;
- fee/FX snapshots;
- opportunities and rejections;
- paper fills;
- positions and settlements;
- configuration version;
- engine version;
- correlation IDs.

### `api/`

Purpose: FastAPI transport only.

The API should expose application services and read models. It must not contain business calculations.

## Canonical identity

A football event should eventually be represented by a deterministic key derived from:

```text
sport + normalized competition + normalized home team + normalized away team + kickoff bucket
```

A canonical market key should contain:

```text
event_key
market_family
period
line/handicap
settlement_fingerprint
```

The venue's own market ID remains attached as source metadata, never as canonical identity.

## Settlement fingerprint

Settlement equivalence is a hard gate.

The fingerprint should explicitly encode, where relevant:

- regulation time vs extra time;
- penalties included/excluded;
- period (full match, first half, second half, etc.);
- line/handicap;
- void/push semantics;
- abandoned/postponed handling when obtainable;
- market-specific resolution rules.

A missing critical rule should lower confidence or make the market paper-review-only rather than being silently assumed.

## Generalized payoff model

The long-term arbitrage representation is a matrix:

```text
                 State A   State B   State C
Leg 1 payoff        ...       ...       ...
Leg 2 payoff        ...       ...       ...
Leg 3 payoff        ...       ...       ...
```

For stake vector `x`, total state payoff is the sum of leg payoff vectors after costs.

An opportunity is a true arbitrage only when:

```text
minimum(net payoff across every valid state) > 0
```

This allows the architecture to grow beyond simple reciprocal-odds checks without redesigning the rest of the system.

## Phase 1 deployment shape

```text
Next.js dashboard
      |
      v
FastAPI backend
      |
      +-- venue adapters
      +-- normalization/matching
      +-- arbitrage + paper engine
      +-- risk/treasury
      |
      v
PostgreSQL (introduced incrementally)
```

Redis is optional and should only be introduced when measured snapshot/update volume warrants it.

## Non-negotiable safety boundary

During Phase 1:

- `SPORTS_HEDGE_MODE` must equal `paper`;
- `SPORTS_HEDGE_EXECUTION_ENABLED` must equal `false`;
- no venue interface exposes real order placement;
- no wallet signing exists;
- no betting credentials are committed;
- no VPN/proxy/geolocation bypass exists.

Detection, simulation and execution remain separate layers so a future live execution component can be added behind an explicit, independently reviewed boundary rather than growing invisibly inside the market-data code.
