# Sports Hedge — Demo Integration / Convergence Task

## Objective

Produce one coherent **tomorrow demo** of Sports Hedge from the current reviewed work without pushing unfinished architecture into `main`.

Work only on branch:

```text
demo/tomorrow-integration
```

The goal is not to implement new product ideas. The goal is to **bring the existing work together into one navigable, internally consistent demo**.

## Product shape

Two top-level product areas:

```text
Arbitrage
Research
```

### Arbitrage demo
Must show a coherent operations-console workflow:

- Near-Arb Watchlist
- Triggered / executable paper arbs
- activity/lifecycle feed
- running paper P&L / capital cards
- native liquidity pools by venue/currency
- Priority Arb Alerts
- manual override / external-manual leg state where relevant
- PAPER MODE / NO EXECUTION labels

Do not represent a price dislocation as a validated arbitrage unless the existing strict arb solver has validated the complete settlement-state economics.

### Research demo
Must show a coherent browse workflow:

```text
Research Home
  -> Matchday / upcoming fixtures
  -> Team Explorer
  -> Team dashboard (Arsenal richest example)
  -> Scenario Lab
  -> Scenario Planner
```

Research must show **odds-weighted value**, not SRC alone:

```text
scenario response
+ manager/regime context
+ sample/confidence/stability
+ model probability
+ effective equivalent market odds
+ implied probability
+ EV / probability edge
+ best venue
+ liquidity/freshness/cost state
```

A high SRC may still be `NO_VALUE`.

## Shared frontend contract — mandatory

Current UI PRs contain conflicting fixture/scenario vocabularies. Create one shared typed demo/read-model contract and migrate the integrated pages to it.

At minimum unify:

- team IDs
- competition IDs
- fixture IDs
- scenario IDs
- response-window encoding
- manager/regime context
- quote/value fields
- demo/live data classification

Use one consistent Arsenal featured fixture across Research Home, Matchday and Team Explorer.

Do not leave contradictory demo data such as Arsenal playing different opponents at the same kickoff.

## Existing PRs to reconcile

Review and selectively integrate useful work from these branches/PRs rather than rewriting everything:

### UI
- PR #44 Team Explorer
- PR #47 Scenario Lab
- PR #42 Matchday Research Hub
- PR #43 Scenario Planner
- PR #51 Research Home
- PR #57 Arbitrage Operations Dashboard
- PR #59 Priority Arb Alert UI

### Backend/read-model candidates
- PR #61 Near-Arbitrage tracker
- PR #60 Priority Arb Alerts
- PR #58 Priority Alert notifications
- PR #66 Event-driven dislocation scanner
- PR #52 Scenario Value Engine

Important: several of the backend PRs have active architecture review comments. Do **not** blindly merge their branches into the demo. If a backend contract is still blocked, use a typed demo adapter/read model behind the same UI seam and clearly label it DEMO / FIXTURE DATA. Preserve the future API shape so the real backend can replace it cleanly.

## Historical data

The demo should expose a historical-data seam and use real normalized historical data if it is safely available from the historical repository work. If PR #35 / #36 are still blocked, do not merge broken identity/provenance architecture simply to make the demo look live.

For the demo:

- prefer Premier League first;
- Championship can be included if data/read-model integration is straightforward;
- show data coverage / source quality visibly;
- synthetic/demo rows must remain explicitly labelled;
- database is source of truth; Excel is export/review only.

## Liquidity pools

Represent native balances separately, e.g.:

```text
Matchbook GBP
Smarkets GBP
Polymarket USD
```

Do not sum USD and GBP without an explicit FX conversion.

Expose:

```text
available
locked
transit
```

where the underlying read model supports it; otherwise use clearly labelled demo pool data.

Capital-source concepts must remain distinct:

```text
AUTO_POOL
MANUAL_OVERRIDE
MANUAL_EXTERNAL
```

`MANUAL_EXTERNAL` means Sports Hedge is waiting for an externally confirmed manual leg; it must not draw from the automated pool or pretend the leg has filled.

## Polymarket / external manual leg

Do not implement geo-circumvention, VPN/proxy logic, or unauthorised execution.

For demo purposes, support the generic state:

```text
AWAITING_EXTERNAL_LEG_CONFIRMATION
```

with an operator action such as:

```text
PROCEED WITH EXTERNAL COUNTERPARTY
```

and fields for later confirmation of:

- venue/product
- executed price
- executed size
- currency
- timestamp
- external reference

After confirmation, future live architecture must revalidate the remaining hedge leg before proceeding. Phase 1 remains paper-only.

## Fee/effective-price rule

Never rank venues only by raw decimal odds.

The integrated demo should visibly support the concept of:

```text
headline odds
-> venue/side cost assumptions
-> effective/net odds
-> implied probability
-> value / arb economics
```

If the real fee engine is not yet ready, use typed demo fee snapshots and label them as assumptions. Unknown costs must not silently become zero.

## Navigation

The demo should feel like one product.

Recommended top-level navigation:

```text
Arbitrage
Research
Paper Portfolio
Treasury (if usable)
```

Research sub-surfaces should be mutually navigable.

## Demo truthfulness

Every component must know its data class:

```text
LIVE / PERSISTED PAPER
HISTORICAL
MODELLED
DEMO / FIXTURE
UNAVAILABLE
```

Never silently fall back from live to fabricated values.

## Acceptance criteria

1. `npm run typecheck` passes.
2. `npm run build` passes.
3. Backend tests/Ruff remain green for any backend files changed on the integration branch.
4. A user can navigate:
   - `/` Arbitrage console
   - `/research`
   - `/matchday`
   - `/teams`
   - `/teams/arsenal`
   - `/scenario-lab`
   - `/scenario-planner`
   - `/arbitrage/priority-alerts`
5. Shared Arsenal fixture/context is consistent across pages.
6. Research surfaces show odds/value context rather than SRC-only ranking.
7. Arbitrage surfaces distinguish Near-Arb, validated arb, Priority Alert and manual-external state.
8. Native-currency pools remain separate.
9. No real bet placement / cancel / wallet-signing path is added.
10. Produce a short `docs/DEMO_READINESS.md` documenting:
    - what is genuinely wired to backend/persisted data;
    - what is historical;
    - what remains fixture/demo;
    - known blockers for the next sprint;
    - exact demo walkthrough.

## Core-tenet review

Before handoff, review the integrated branch against `docs/CORE_TENETS.md` and report PASS / PARTIAL / FAIL for each materially relevant tenet.

Stop for architect review. Do not merge to `main`.
