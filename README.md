# Sports Hedge

Sports Hedge is an internal football arbitrage research and paper-trading platform.

## Phase 1

Phase 1 is deliberately **read-only and paper-only**. It ingests market data, normalizes football markets across venues, models executable liquidity and costs, detects arbitrage, and simulates fills. It contains no real order-placement capability.

Initial venue plan:

- **Matchbook** — primary official API integration for football prices and liquidity.
- **Polymarket** — permitted public/read-only market data for research and paper simulation; execution disabled.
- **Smarkets** — adapter reserved for a later phase.

The paper-only boundary is enforced in both configuration and the venue interface. Phase 1 has no `place_order` or `cancel_order` methods.

## Repository layout

```text
backend/
  src/sports_hedge/
    api/        internal FastAPI status/control surface
    domain/     canonical market models
    venues/     read-only venue integrations
    facts/      canonical match/team/competition identity
    historical/ football match facts repository (canonical denominator)
    odds/       historical odds observations, coverage, Excel export
  tests/
docs/
  CORE_TENETS.md
  core-tenets/
.github/workflows/
```

## Backend setup

Requires Python 3.12+.

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` at repository root and add Matchbook credentials locally when available. Never commit credentials.

Run tests:

```bash
cd backend
python -m pytest -q
```

Run the internal API:

```bash
cd backend
uvicorn sports_hedge.api.main:app --reload
```

Then inspect:

- `GET /health` — confirms paper mode and execution-disabled state.
- `GET /venues` — shows current venue capability flags.

## Current implementation

- paper-only safety configuration;
- canonical venue/event/market/order-book models;
- read-only venue abstraction;
- Matchbook official API login + event/market/order-book reads;
- Polymarket public Gamma/CLOB event/market/order-book reads;
- automated tests proving execution is unavailable;
- backend CI for compile, tests and static correctness.

## Project documents

**Start with `docs/CORE_TENETS.md`.** The core-tenets directory is the product/architecture contract for humans and AI agents and should be used as acceptance criteria during implementation and review.

See also:

- `docs/ARCHITECTURE.md`
- `docs/INITIAL_INTEGRATION_PLAN.md`
- `docs/RESEARCH_MODULE.md`
- `docs/MARKET_INTELLIGENCE_MODULE.md`
- `docs/SCENARIO_RESPONSE_PROFILES.md`
- `docs/MANAGER_ERA_AND_REGIME_CONTEXT.md`
- `docs/PRIORITY_ARB_ALERTS.md`
- `docs/HISTORICAL_ODDS.md` — source-neutral historical odds repository (Smarkets optional; quality tiers; coverage and Excel export)
