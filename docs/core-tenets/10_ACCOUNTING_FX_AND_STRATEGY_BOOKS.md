# Core Tenet 10 — Accounting, FX & Strategy Books

## Principle

Sports Hedge uses one audited, append-only double-entry accounting system with GBP as functional/presentation currency, while preserving native venue/currency balances and separate strategy reporting.

## Strategy books

User-facing modules:

```text
Arbitrage
Research
```

Accounting strategy dimensions:

```text
ARBITRAGE
RESEARCH_VALUE
```

`RESEARCH_VALUE` represents positions generated from Research signals; Research analysis itself is not profit.

Do not create disconnected ledgers for the two strategies.

## Native liquidity and GBP valuation

Each posting should retain native amount, currency, GBP carrying value and rate/provenance where applicable.

Examples:

```text
ASSET:CASH:AVAILABLE:<venue>:<currency>
ASSET:CASH:LOCKED:<venue>:<currency>
ASSET:CASH:TRANSIT:<venue>:<currency>
PNL:BETTING
PNL:VENUE_FEES
PNL:FX:REALISED
PNL:FX:UNREALISED
```

GBP values must come from the FX layer; never assume USD=GBP or directly sum native currencies.

## FX concepts

Keep distinct:

1. actual executed conversion rate for realised FX;
2. daily accounting valuation rate;
3. independent reconciliation/check rate.

Current design uses ECB reference rates as the primary daily accounting source and Bank of England as an independent check where available. Non-working days carry forward the most recent published close with provenance; no synthetic rate should be invented.

## Audit discipline

- Journals balance exactly in GBP.
- `(source, source_id)` or equivalent idempotency prevents duplicate posting.
- Corrections use reversals, not destructive edits.
- Treasury native balances reconcile to venue balances before unexplained adjustments/revaluation.
- Shared/unattributable treasury FX should remain shared/unallocated rather than being forced into a strategy.

## P&L presentation

Keep strategy result and treasury FX effects distinguishable, conceptually:

```text
betting/trading realised P&L
- venue fees
+ realised FX
+ unrealised FX
= total GBP P&L
```

## Review checks

- Is there still one ledger/source of truth?
- Are ARBITRAGE and RESEARCH_VALUE separable by dimension?
- Are native balances preserved independently from GBP presentation values?
- Are FX source/rate/date/status retained?
- Are corrections auditable rather than overwritten?
