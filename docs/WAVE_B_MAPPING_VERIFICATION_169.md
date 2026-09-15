# Wave B lane — Mapping verification (#169)

Frozen base: `integration/wave-b-2026-09-15` @ `30c54ce9a7ac839111a7dc73ac3e0081d7bb6505`.

This lane implements deterministic human-in-the-loop mapping learning. It does **not** merge into Wave B, #179, #131, or `main`.

## Owns

- Learned mapping rule models, inference, and application (`matching/learned_rules.py`)
- Additive SQLite mapping-review / rule store (`persistence/mapping_rules.py`)
- Review service + ChatGPT prompt packaging (`application/mapping_review.py`)
- Narrow review API (`api/mapping_reviews.py`, mounted in `api/main.py`)
- Reusable frontend verification panel seam (`components/mapping-verification-panel.tsx`, `lib/mapping-verification.ts`)

## Deliberately not owned

- **#168 Opportunity Monitor** final Verify placement / layout. The panel is exported as a seam and is not wired into `app/page.tsx`.
- **#165 current-market** merge/count/headline logic. No edits to fixture inventory aggregation.
- **#163 audit Age / sorting**. No edits to paper-scan history ordering.

## Minimal integration hooks (for the Wave-B integrator)

These are the only production-path touches outside the new files:

1. `config.py` — `mapping_rules_db_path` (SQLite, local paper MVP).
2. `application/paper_scan.py` — optional `mapping_rule_store` so `PaperScanService` builds `EventMatcher`/`MarketMatcher` with the learned applicator.
3. `api/paper.py` — `get_paper_scan_service()` injects `get_mapping_rule_store()`; `_collect_report()` passes the same matcher into `ReadOnlyCrossVenueCollector`.
4. `matching/events.py` / `matching/markets.py` — existing canonical matching seam; learned rules apply *before* native scoring and **cannot** override settlement/period/line/outcome mismatch.
5. `api/main.py` — include mapping-review router.

No second identity store. `#161` `FixtureCurrentStateStore` aliases are unchanged.

## Safety

- ChatGPT text never activates a rule.
- `AMBIGUOUS` / `NOT VERIFIED` persist the review only.
- `VERIFIED` + explicit confirmation still cannot activate a rule when the candidate evidence has competition, kickoff, family, period, line, settlement, or outcome conflicts. The review is persisted with `activation_blocked_reason`; no enabled rule is written.
- Disabled/revoked rules stop applying; paper-scan audit rows are not rewritten.
- Learned mapping alone does not bypass quote freshness, fees, FX, depth, risk, solver, allocator, treasury, or venue-participation gates.
