# ADR 0002 — Adaptive dual-cadence scanner

**Status:** Accepted direction (Issue #158, architect review `5196716600`). Design only until implementation is authorised. Do not merge to `main`. Do not modify #131. Do not land scanner/coordinator code until the #118 child (#157) has owner-Windows acceptance.

**Date:** 14 September 2026 (revised same day after architect review)

**Implementation base:** PR #157 head `51fbd24034654bb9e05ca413c8707f1d9d4843ac` (`cursor/scan-soft-budget-finalisation-afe6`), child of #131 head `6fc68e97bb248ea0392f569ea54764464a23b868`.

## Context

The live paper scanner is a single 30s loop (`LiveRefreshCoordinator`) that runs one monolithic `collect_and_scan()` over the in-scope fixture universe (`max_event_pairs` default 60, 45s soft budget, 5s coordinator grace on #157). That loop can spend the entire budget evaluating distant fixtures while near-kickoff and in-play markets — the dislocation window — wait for the next cycle.

Issue #158 asks for **one scheduler with two coordinated cohorts**, not two independent scanners.

## Decision

1. Keep a single `LiveRefreshCoordinator` process and a single canonical fixture/market identity store (**process memory for v1**). Restart honesty: Tracked empty until a collection completes; **UNIVERSE bootstrap is due immediately** on startup. SQLite fixture-inventory persistence is later, not this implementation.
2. Split work into **HOT** (30s cadence; truthful in-play + ≤60 minutes pre-kickoff + bounded post-kickoff unknown) and **UNIVERSE** (180s generation cadence; 150s per-generation work budget).
3. **HOT collector timeout is 25s**, not 45s. Reuse #157’s 4s leftover reserve + 5s coordinator grace so the worst-case envelope is ~30s. HOT must not overlap itself.
4. **150s is a UNIVERSE generation budget, not one continuous job.** Each scheduler run processes a resumable chunk only until `next_hot_due - safety_margin`, persists cursor/progress, yields, lets HOT run, then resumes. A generation must make forward progress across multiple HOT cycles and must not starve HOT.
5. HOT preempts UNIVERSE. HOT ordering uses the **simple deterministic key** (truthful in-play → nearest kickoff → current opportunity state). Do not couple the dislocation burst scheduler.
6. Kickoff-passed + unknown in-play stays HOT **without a live label**, only within a **3h** post-kickoff uncertainty window. After that, leave HOT scheduling unless a provider explicitly says in-running. Do not fabricate completed or live from elapsed time.
7. Radar TTL: **HOT 90s / UNIVERSE 360s**. Executable quote freshness remains the existing fail-closed ~1s contract.
8. Replace `Tracked = latest completed collection cohort` with a **per-identity current-state merge**. Qualifying arbs from either lane surface immediately.
9. Explicit `POST /paper/collect` remains the current **45s UNIVERSE-shaped** diagnostic/manual contract while #157 is being accepted.
10. Expose Fast scan and Full sweep as distinct operator status.

Detailed plan, seams, risks, migration, and acceptance tests: [`docs/DUAL_CADENCE_SCANNER.md`](../DUAL_CADENCE_SCANNER.md).

## Consequences

### Positive

- Near-kickoff/live books refresh on a 30s lane whose collector budget can actually finish inside that cadence.
- Distant fixtures stay on radar; UNIVERSE work survives HOT preemption via chunked resume.
- Reuses #157 leftover/partial-finalisation instead of racing it.
- Preserves one canonical identity (Tenet 03) and paper-only venues (Tenet 02).

### Negative

- Tracked API semantics change. Existing tests that assert latest-cohort-only membership must be rewritten, not loosened.
- Two lanes sharing Matchbook/Polymarket/Kalshi can double rate-limit pressure unless HOT skips rediscovery and a shared provider gate exists.
- Operator UI and `/paper/live-refresh` must grow nested lane status; a single `Last scan` becomes dishonest.

### Non-decisions (out of scope)

- Live execution, new venues, settlement redesign, treasury redesign, burst-mode rewrite, reducing `max_event_pairs`, stretching HOT to minutes, SQLite fixture-inventory persistence, giving explicit `POST /paper/collect` the 150s generation budget while #157 is still in flight.
