# ADR 0001 — Start as a Modular Monolith

**Status:** Accepted  
**Date:** 11 September 2026

## Context

Sports Hedge needs several distinct capabilities—venue ingestion, normalization, market matching, arbitrage solving, liquidity modelling, risk scoring, paper execution, treasury and persistence—but Phase 1 is still a research product with a small number of venues and no live order execution.

Splitting these capabilities into networked microservices now would increase deployment, observability and consistency complexity before there is evidence that independent scaling is required.

At the same time, placing all logic in API routes or venue clients would make future growth difficult.

## Decision

Implement the backend as a **modular monolith**.

Each business capability has its own package and explicit responsibility. Cross-module interactions happen through domain models and narrow interfaces. Transport and persistence implementations sit at the edges.

Initial backend packages:

```text
sports_hedge/
  api/
  application/
  arbitrage/
  domain/
  fees/
  liquidity/
  matching/
  normalization/
  paper/
  persistence/
  risk/
  treasury/
  venues/
```

## Consequences

### Positive

- fastest route to a working paper product;
- simple local development and testing;
- one consistency boundary for paper positions and P&L;
- easy refactoring while market assumptions are still changing;
- modules can later be extracted if measured load warrants it.

### Negative

- one process initially shares failure and scaling boundaries;
- discipline is required to prevent imports that bypass module ownership;
- high-frequency ingestion may eventually need dedicated workers.

## Extraction rule

A module should only become a separate service when there is measured evidence for one of:

- materially different scaling requirements;
- independent availability requirements;
- process isolation required for execution safety;
- deployment cadence conflict;
- persistent performance contention.

Live order execution, if later introduced, is the most likely first component to warrant stronger isolation.
