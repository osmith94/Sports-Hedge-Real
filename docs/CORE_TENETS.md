# Sports Hedge — Core Tenets

## Purpose

This directory is the product contract for Sports Hedge.

Every agent, PR, architectural review and release review should compare the implementation against these tenets. If a proposed change conflicts with a core tenet, the conflict must be called out explicitly rather than silently accepted.

These files summarize the non-negotiable product principles agreed for Sports Hedge. Detailed feature specifications remain in the wider `docs/` directory.

## Core tenets

1. [`core-tenets/01_PRODUCT_STRUCTURE.md`](core-tenets/01_PRODUCT_STRUCTURE.md) — two distinct top-level product modules: Arbitrage and Research.
2. [`core-tenets/02_PAPER_MODE_AND_EXECUTION_BOUNDARIES.md`](core-tenets/02_PAPER_MODE_AND_EXECUTION_BOUNDARIES.md) — Phase 1 is read-only toward venues and paper-only.
3. [`core-tenets/03_CANONICAL_MARKET_EQUIVALENCE.md`](core-tenets/03_CANONICAL_MARKET_EQUIVALENCE.md) — executable cross-venue comparison is strict: both markets must be the same approved archetype with matching required parameters and compatible settlement states; ambiguous contracts are reviewed, not guessed.
4. [`core-tenets/04_ARBITRAGE_OPERATIONS.md`](core-tenets/04_ARBITRAGE_OPERATIONS.md) — arbitrage is depth-, cost- and risk-aware, with near-arb lifecycle visibility.
5. [`core-tenets/05_RESEARCH_AND_VALUE.md`](core-tenets/05_RESEARCH_AND_VALUE.md) — Research is probabilistic and all actionable signals are weighted against equivalent market odds.
6. [`core-tenets/06_SCENARIO_RESPONSE_PROFILES.md`](core-tenets/06_SCENARIO_RESPONSE_PROFILES.md) — scenario analysis is team-specific, league-benchmarked and sample-aware.
7. [`core-tenets/07_MANAGER_AND_REGIME_CONTEXT.md`](core-tenets/07_MANAGER_AND_REGIME_CONTEXT.md) — analysis must distinguish different managerial/team regimes.
8. [`core-tenets/08_HISTORICAL_DATA_AND_PROVENANCE.md`](core-tenets/08_HISTORICAL_DATA_AND_PROVENANCE.md) — historical data must retain provenance, quality and uncertainty.
9. [`core-tenets/09_LIQUIDITY_CAPITAL_AND_PRIORITY_ALERTS.md`](core-tenets/09_LIQUIDITY_CAPITAL_AND_PRIORITY_ALERTS.md) — capital is managed by native venue/currency pools; sizing, reserve, lock duration, unwindability and capital recycling are first-class economics, with explicit manual escalation for exceptional arbs.
10. [`core-tenets/10_ACCOUNTING_FX_AND_STRATEGY_BOOKS.md`](core-tenets/10_ACCOUNTING_FX_AND_STRATEGY_BOOKS.md) — one audited ledger, GBP functional currency, separate Arbitrage and Research Value strategy books.
11. [`core-tenets/11_UI_AND_DATA_HONESTY.md`](core-tenets/11_UI_AND_DATA_HONESTY.md) — users must always know what is live, historical, modelled or demo data.
12. [`core-tenets/12_AGENT_REVIEW_CONTRACT.md`](core-tenets/12_AGENT_REVIEW_CONTRACT.md) — agents must review their work against all applicable tenets before handoff.
13. [`core-tenets/13_EVENT_INTELLIGENCE_AND_CAUSALITY.md`](core-tenets/13_EVENT_INTELLIGENCE_AND_CAUSALITY.md) — event timing/lead-lag analysis is valuable context but must not be overstated as causation.
14. [`core-tenets/14_EVENT_DRIVEN_PRICE_DISLOCATION_ARBITRAGE.md`](core-tenets/14_EVENT_DRIVEN_PRICE_DISLOCATION_ARBITRAGE.md) — a key arbitrage thesis is rapid detection of temporary cross-venue disagreement after material sporting events, with strict freshness, depth, settlement and cost controls.
15. [`core-tenets/15_EFFECTIVE_VENUE_ECONOMICS_AND_FEES.md`](core-tenets/15_EFFECTIVE_VENUE_ECONOMICS_AND_FEES.md) — Sports Hedge compares net executable economics after the exact applicable venue/market/side/order-role costs, not headline odds.
16. [`core-tenets/16_EXTERNAL_MANUAL_LEGS.md`](core-tenets/16_EXTERNAL_MANUAL_LEGS.md) — opportunities with a required non-automated venue leg must hard-stop for explicit external/manual confirmation, keep capital separate, and revalidate the remaining hedge before proceeding.
17. [`core-tenets/17_HISTORICAL_MARKET_MOVEMENT_CONTEXT.md`](core-tenets/17_HISTORICAL_MARKET_MOVEMENT_CONTEXT.md) — large or rapid market moves should be interpreted against comparable historical team/regime/league movements, with explicit sample size, uncertainty and the ability to report weak/no relationship or no historical precedent.
18. [`core-tenets/18_EXECUTION_ATOMICITY_AND_FILL_RISK.md`](core-tenets/18_EXECUTION_ATOMICITY_AND_FILL_RISK.md) — one-leg-filled / remaining-leg-failed exposure is a principal production risk; executable opening liquidity is current taker depth we can consume now, not passive/maker quotes; real execution requires fresh revalidation after every fill, hard unhedged-exposure controls, shadow execution evidence and staged micro-live validation before treasury scale increases.
19. [`core-tenets/19_CONCURRENT_HOT_AND_UNIVERSE_SCANNING.md`](core-tenets/19_CONCURRENT_HOT_AND_UNIVERSE_SCANNING.md) — HOT and UNIVERSE are independent concurrent scanner workers: HOT refreshes priority fixtures rapidly while UNIVERSE completes a durable full sweep in the background; HOT may receive provider-request priority but must never terminate, reset or restart UNIVERSE, and UNIVERSE must stream results and promotions incrementally.
20. [`core-tenets/20_APPROVED_MARKET_CATALOGUE_AND_EXCEPTION_REVIEW.md`](core-tenets/20_APPROVED_MARKET_CATALOGUE_AND_EXCEPTION_REVIEW.md) — the operational arbitrage scanner searches a deliberately bounded catalogue of pre-approved football market archetypes; only exact approved-equivalent contracts enter the solver, while ambiguous probable-archetype markets become review exceptions and unsupported novelty markets are filtered.

## Source specifications

These tenets should be read alongside:

- `ARCHITECTURE.md`
- `INITIAL_INTEGRATION_PLAN.md`
- `MARKET_INTELLIGENCE_MODULE.md`
- `RESEARCH_MODULE.md`
- `SCENARIO_RESPONSE_PROFILES.md`
- `MANAGER_ERA_AND_REGIME_CONTEXT.md`
- `PRIORITY_ARB_ALERTS.md`
- `DUAL_CADENCE_SCANNER.md` — historical/implementation scanner specification. Core Tenet 19 is authoritative for HOT/UNIVERSE concurrency and supersedes any serialized or yield-to-HOT behaviour that prevents a continuous independent UNIVERSE sweep.
- Core Tenets 03 and 20 are authoritative for market-contract admission. Any older specification proposing generic confidence-scored market equivalence as permission to enter the solver is superseded. Confidence may still be used for fixture identity and review prioritisation; executable market equivalence is catalogue-based and deterministic.
- future accounting / FX / historical-data specifications

## Review rule

A feature is not complete merely because it compiles or looks correct in isolation.

Before acceptance, ask:

> Does this implementation preserve every applicable Sports Hedge core tenet?

If not, the PR should document the deviation and be changed or explicitly approved before merge.
