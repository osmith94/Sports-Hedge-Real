# Core Tenet 01 — Product Structure

## Principle

Sports Hedge has two distinct top-level product modules:

```text
Arbitrage
Research
```

They may share canonical event/market identities, venue adapters, treasury and accounting infrastructure, but their purpose, terminology and user journeys must remain distinct.

## Arbitrage

Arbitrage answers:

> Where does a positive minimum payoff exist across all valid settlement states after known costs, executable depth and risk constraints?

Its UI should behave like an operations console: near opportunities, triggered opportunities, fill/settlement activity, capital and liquidity.

## Research

Research answers:

> What historically happens in this football context, how does this team respond, and does the current equivalent market price imply value?

Its UI should support Football-Manager-style browsing through fixtures, teams, scenarios, manager eras, historical trends and odds-weighted signals.

## Non-negotiables

- Research value must never be called arbitrage or guaranteed profit.
- Arbitrage must never depend on a directional football prediction.
- The Research Home should surface odds-weighted value signals, upcoming fixtures and browse paths.
- Team and fixture pages should drill naturally into Scenario Lab / manager-era analysis.
- The Arbitrage workspace remains separately accessible and operational.

## Violation examples

- Showing an SRC signal under a heading called `Guaranteed Arbitrage`.
- Mixing near-arb lifecycle events into a team research profile as if they were football trends.
- Building two incompatible event identity systems for the two modules.

## Review checks

- Can a reviewer clearly identify which module owns every new feature?
- Is probabilistic Research language distinct from guaranteed-arb language?
- Do shared objects use common canonical identity rather than duplicate models?
- Does the navigation preserve a clear Arbitrage vs Research split?
