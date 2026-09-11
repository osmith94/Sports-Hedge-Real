# Core Tenet 16 — External Manual Legs

## Principle

Sports Hedge may identify an otherwise valid multi-venue opportunity where one leg cannot or should not be executed automatically by Sports Hedge. That leg must enter an explicit **external/manual confirmation workflow** rather than being treated as filled, funded from an automated pool, or silently skipped.

This is a generic execution-state design. It is not a geographic-access workaround and must not implement VPN/proxy/geobypass logic or instruct a person to violate a venue's rules.

## Required states

At minimum, the system must be able to represent:

```text
MANUAL_EXTERNAL
AWAITING_EXTERNAL_LEG_CONFIRMATION
EXTERNAL_LEG_CONFIRMED
EXTERNAL_LEG_REJECTED
EXTERNAL_LEG_EXPIRED
```

`MANUAL_EXTERNAL` is a capital/execution-source classification, not an automated liquidity pool.

## Non-negotiables

1. **Hard stop before automated commitment**
   - If a required leg is `MANUAL_EXTERNAL`, Sports Hedge must not represent the complete trade as filled or guaranteed.
   - The automated side must not be committed merely because the external side is expected to be placed.

2. **No automated-pool draw for the external leg**
   - External/manual capital is separate from `AUTO_POOL` and `MANUAL_OVERRIDE` capital.
   - Native venue/currency requirements remain visible.

3. **Explicit operator confirmation**
   - The UI may offer an action such as:

```text
PROCEED WITH EXTERNAL COUNTERPARTY
```

   - This means "prepare/acknowledge the external leg workflow"; it does not place the bet.

4. **Confirmation record required**
   A confirmed external leg must record, where available:

```text
venue / product
selection / outcome
executed price
executed size
currency
execution timestamp
external reference
operator note / confirmation source
```

5. **Revalidation before the remaining hedge leg**
   - After external confirmation, Sports Hedge must re-fetch/revalidate the remaining leg(s): price, depth, settlement equivalence, fees, FX, freshness and risk.
   - If the hedge has deteriorated, the workflow must not pretend the original arbitrage still exists.

6. **Expiry and abandonment**
   - Manual-external opportunities must expire when quotes are stale, market state changes, or the confirmation window is exceeded.
   - Abandoned/expired attempts remain auditable.

7. **Venue and jurisdiction neutrality**
   - The state applies to any venue requiring manual/external handling, not only Polymarket.
   - Sports Hedge must not assume that a particular person, account, location or private arrangement is permitted by a venue. Eligibility/compliance sits outside the paper workflow and must be checked separately before any real-money phase.

## Polymarket example

A paper-mode opportunity may contain:

```text
Leg A — Matchbook GBP — automatically modelled/read-only leg
Leg B — Polymarket USD — MANUAL_EXTERNAL
```

The UI should show:

```text
AWAITING EXTERNAL LEG CONFIRMATION
Polymarket · USD
Target price: ...
Target size: ...
Maximum age / expiry: ...
```

The opportunity does **not** become a completed arbitrage until the external leg is confirmed and the remaining hedge is revalidated.

## What would violate this tenet

- Treating an unconfirmed external leg as filled.
- Deducting the external leg from an automated liquidity pool.
- Automatically executing another leg while the required external leg remains unconfirmed, unless an explicitly modelled and approved execution sequence proves that risk is acceptable.
- Using a VPN/proxy or location-masking workflow to bypass venue restrictions.
- Calling an opportunity "guaranteed" after one side has moved or expired.
- Recording only a yes/no confirmation without executed size, price and timestamp.

## Review checks

A reviewer should verify:

- [ ] `MANUAL_EXTERNAL` is distinct from `AUTO_POOL` and `MANUAL_OVERRIDE`.
- [ ] External-required opportunities have a hard-stop state.
- [ ] No external leg is marked filled before explicit confirmation.
- [ ] Confirmation captures executed economics and provenance.
- [ ] Remaining legs are revalidated after confirmation.
- [ ] Stale/expired manual workflows fail closed.
- [ ] No geographic/access-control bypass exists.
- [ ] Paper/demo screens clearly label the workflow as paper/manual and not real execution.
