# Sports Hedge

Sports Hedge is an internal football arbitrage research and paper-trading platform.

## Phase 1

Phase 1 is deliberately **read-only and paper-only**. It ingests market data, normalizes football markets across venues, models executable liquidity and costs, detects arbitrage, and simulates fills. It contains no real order-placement capability.

Initial venue plan:

- **Matchbook** — primary official API integration for football prices and liquidity.
- **Polymarket** — permitted public/read-only market data for research and paper simulation; execution disabled.
- **Smarkets** — adapter reserved for a later phase.

See `docs/SPORTS_HEDGE_MVP.md` and `docs/INITIAL_INTEGRATION_PLAN.md` for the product and engineering briefs.
