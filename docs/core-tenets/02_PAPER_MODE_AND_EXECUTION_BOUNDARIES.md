# Core Tenet 02 — Mode and execution boundaries

## Principle

Sports Hedge Real has two operating modes.

- `SPORTS_HEDGE_MODE=paper` simulates fills. It does not place venue orders.
- `SPORTS_HEDGE_MODE=real` is the live-execution seam. It stays disarmed unless `SPORTS_HEDGE_EXECUTION_ENABLED=true`.

The default configuration is paper mode with execution disabled.

Catalogue admission is not a mode switch. A relationship admitted by the Approved Match Register is execution-eligible at the catalogue gate. A historical paper label on that relationship is not an independent veto. Venue orders still require all of:

- mode `real`
- execution explicitly enabled
- exact native IDs
- an accepted Price-2 / fill plan
- venue transport readiness
- fee, FX, depth, and risk evidence
- duplicate protection and the existing fill-safety controls

Unregistered, unsupported, parameter-mismatched, and contradictory relationships stay fail-closed.

## Historical note

The original Phase 1 product was read-only and paper-only, with no order placement. That history is why some stored labels still say `paper_assumed_equivalent`. Those labels remain readable. They do not describe the current catalogue gate.

## Non-negotiables

- Do not enable execution as part of a wording or catalogue cleanup.
- Do not add a venue write call to make a demo button work.
- Do not treat a registered relationship as an order.
- Do not bypass native IDs, settlement contradictions, or economic evidence.
- Polymarket BUY readiness, tracked separately from this cleanup, asks only whether the exact BUY can execute: sufficient collateral and sufficient allowance for the actual required exchange_v3 spender. A generic platform-wide `is_fully_approved` flag must not veto that BUY because unrelated perps or auto-redeem permissions are missing. See `docs/POLYMARKET_BUY_READINESS.md`.
- UI must distinguish simulated paper fills from live execution.
- No VPN/proxy/geolocation bypass or access-control circumvention.

## Violation examples

- Setting `SPORTS_HEDGE_EXECUTION_ENABLED` in order to finish a catalogue cleanup.
- A `PLACE BET` button wired to a venue API without the real-mode gates.
- Rejecting an Approved Match Register relationship only because a stored label says paper.
- Treating a manually prepared Priority Arb ticket as executed.

## Review checks

- Search the change for order placement, cancellation, and signing.
- Confirm the default configuration still reports execution disabled.
- Confirm paper fill records are clearly simulated.
- Confirm catalogue eligibility follows registration and still fail-closes unregistered or contradictory contracts.
