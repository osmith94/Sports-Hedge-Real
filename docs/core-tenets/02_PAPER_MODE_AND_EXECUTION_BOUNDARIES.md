# Core Tenet 02 — Paper Mode & Execution Boundaries

## Principle

Phase 1 is **read-only toward venues and paper-only for execution**.

The system may discover, normalize, compare, simulate, reconcile and alert. It must not place, cancel or sign real wagers/orders.

## Non-negotiables

```text
SPORTS_HEDGE_MODE=paper
SPORTS_HEDGE_EXECUTION_ENABLED=false
```

- Venue interfaces expose market-data capabilities only in Phase 1.
- No `place_order`, `cancel_order`, wallet signing or trading-auth methods belong in Phase 1 venue contracts.
- No VPN/proxy/geolocation bypass or access-control circumvention.
- Manual override workflows may prepare/recalculate tickets but stop before real venue execution.
- Priority alerts do not weaken the paper-only boundary.
- UI controls must visibly state PAPER MODE where an operator could reasonably confuse simulation with live execution.

## Violation examples

- Adding a live Smarkets/Matchbook order-submit method to make a demo button work.
- A `PLACE BET` button wired to a venue API.
- Treating a manually prepared Priority Arb ticket as executed.

## Review checks

- Search the change for order placement/cancellation/signing capabilities.
- Confirm capability flags still report execution disabled.
- Confirm paper fill records are clearly simulated.
- Confirm any manual workflow ends before a real venue call.
