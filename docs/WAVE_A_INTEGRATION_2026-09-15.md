# Wave A combined integration — 2026-09-15

This branch is the controlled combined-integration gate for Wave A on top of frozen checkpoint:

- base: `integration/foundations-1-5-2026-09-15`
- frozen base SHA: `dfda32e0a3c71a6fadc3c38a61ad12cbfd986d7f`

Accepted exact heads, to be composed in this order:

1. PR #176 / Item 10 — `fa1f2ae343f1e3eff411486ec1e9a0bbebc5ede6`
2. PR #175 / Items 1+2 — `673d9905ce448354d13fc012c86cff1d0b460003`
3. PR #177 / Item 5 — `1fb69b74f3938c468ad089e042b904c0d898849b`
4. PR #178 / Item 3 — `9db1ce7d487a98567af8d35ce25ce7fb139e0f0d`

## Integration rules

- Preserve the accepted behavior of every exact head; do not take overlapping files wholesale from one lane.
- Resolve overlap deliberately in `paper.py`, `collector.py`, `live_refresh.py`, `fixture_current_state.py`, frontend API/status surfaces, and associated tests.
- Keep one `LiveRefreshCoordinator` and one `FixtureCurrentStateStore`.
- Preserve HOT 25s collector / <=30s coordinator envelope and graceful partial semantics.
- Preserve production-path idempotency after scheduled persistence failure/retry.
- Preserve current-radar lifecycle eviction and Matchbook lifecycle provenance, including `graded` terminal handling.
- Preserve independent Fast Scan / Full Sweep venue participation, fail-closed malformed persisted settings, and truthful all-off state.
- Preserve Paper scan history as append-only audit with the latest-100 UI/window semantics; do not repurpose audit storage as current radar.
- Preserve Phase 1 paper-only/read-only venue boundaries. No place/cancel/sign/write or geobypass.
- Do not merge to `main` or PR #131.

## Required combined gate

Before architect handoff:

1. Run full backend tests and Ruff F.
2. Run frontend tests, typecheck, and production build.
3. Add focused combined regressions where overlap could otherwise regress accepted behavior.
4. Review against all applicable Core Tenets, especially 02, 03, 04, 08, 11, 12, 14, 15, 16 and 18.
5. State data provenance honestly and list any partial/deferred items or conflicts.
6. Stop for architect review on the exact integrated SHA.
