# ADR 0002 — Adaptive dual-cadence scanner

**Status:** Proposed (Issue #158). Design only. Do not implement until architect review. Do not merge to `main`. Do not modify #131. Do not land on the #118 child (#157) until that timeout correction is accepted.

**Date:** 14 September 2026

**Implementation base:** PR #157 head `51fbd24034654bb9e05ca413c8707f1d9d4843ac` (`cursor/scan-soft-budget-finalisation-afe6`), child of #131 head `6fc68e97bb248ea0392f569ea54764464a23b868`.

## Context

The live paper scanner is a single 30s loop (`LiveRefreshCoordinator`) that runs one monolithic `collect_and_scan()` over the in-scope fixture universe (`max_event_pairs` default 60, 45s soft budget, 5s coordinator grace on #157). That loop can spend the entire budget evaluating distant fixtures while near-kickoff and in-play markets — the dislocation window — wait for the next cycle.

Issue #158 asks for **one scheduler with two coordinated cohorts**, not two independent scanners.

## Decision

1. Keep a single `LiveRefreshCoordinator` process and a single canonical fixture/market identity store.
2. Split work into **HOT** (default 30s; in-play where provider truth exists + ≤60 minutes pre-kickoff) and **UNIVERSE** (default 180s; full captured universe, resumable).
3. HOT preempts UNIVERSE. UNIVERSE must yield between clusters when HOT is due.
4. Replace `Tracked = latest completed collection cohort` with a **per-identity current-state merge** and lane-specific radar TTL. Executable/TRIGGERED freshness stays fail-closed.
5. Expose Fast scan and Full sweep as distinct operator status. Do not hide a far-future qualifying arb because it was found on UNIVERSE.

Detailed plan, seams, risks, migration, and acceptance tests: [`docs/DUAL_CADENCE_SCANNER.md`](../DUAL_CADENCE_SCANNER.md).

## Consequences

### Positive

- Near-kickoff/live books refresh on a bounded 30s lane without forcing T+6d fixtures through the same cycle.
- Distant fixtures stay on radar for initial venue pricing / later dislocations.
- Reuses #157 leftover/partial-finalisation instead of racing it.
- Preserves one canonical identity (Tenet 03) and paper-only venues (Tenet 02).

### Negative

- Tracked API semantics change. Existing tests that assert latest-cohort-only membership must be rewritten, not loosened.
- Two lanes sharing Matchbook/Polymarket/Kalshi can double rate-limit pressure unless HOT skips rediscovery and a shared provider gate exists.
- Operator UI and `/paper/live-refresh` must grow nested lane status; a single `Last scan` becomes dishonest.

### Non-decisions (out of scope)

- Live execution, new venues, settlement redesign, treasury redesign, burst-mode rewrite, reducing `max_event_pairs`, stretching HOT to minutes.
