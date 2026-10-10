# Wave 39 — Price-2 bounded Matchbook tail refresh after slow Polymarket

> **Operator limit correction (2026-10-10):** The operator has confirmed that Max Event / Max Opportunity / Max One-Time are existing persisted dashboard settings (shown as £7/£7/£5 in a PAPER-mode screenshot), NOT hard-coded canary limits. Any earlier reference in this spec to £3/£5 or fixed £5 event/opportunity test ceilings is superseded. This wave must use actual effective operator settings from `effective_operator_scanner_settings(settings)` and existing `policy_from_settings`, never override, add a separate session cap, or modify runtime deployment controls. This wave is not authorised to turn on live execution. See updated [Wave 40 contract](WAVE40_LIVE_CANARY.md) on PR #44's branch.


## Observed incident — Real Madrid v Villarreal, 2026-10-10

One live-data dry-run price-2 candidate calculated 5.83% net, £0.29 *theoretical* guaranteed profit, but rejected `execution_reprice_skew`. Audit:
- Matchbook DRAW retrieved 21:32:04.367, quote age 4,467 ms at evaluation;
- Polymarket HOME retrieved 21:32:07.054;
- Polymarket AWAY retrieved 21:32:07.091;
- final retrieval skew **2,725 ms**, against configured **500 ms** maximum; oldest quote also older than **2,000 ms** entry freshness gate.
- Quote evaluation was about 4.7 seconds (NOT exchange order-submission time).
- This is a *candidate*, not proven executable/guaranteed profit. Never present the 5.83% / £0.29 as a fill.

Code in `backend/src/sports_hedge/application/price_engine.py`:
`_reprice_exact_books` calls `_execution_fetch_venues` concurrently using `asyncio.gather`. `_execution_fetch_matchbook` and `_execution_fetch_polymarket` individually use existing targeted native-ID provider scheduler. `_finish_execution_reprice` constructs one `ExecutionSnapshot` then compares `snapshot.skew_exceeded()`. There is currently NO late-venue-tail recovery; this rejects even if a second, cheap exact-ID Matchbook fetch after Polymarket completes would make every constituent contemporaneous.

## Requested solution (strictly bounded, no price safety relaxation)

- For **Matchbook + Polymarket** Price-2 only: perform existing simultaneous initial native-book reads. If all required books are complete and PM was materially later so **the earliest/limiting book is Matchbook**, and initial retrieval skew exceeds existing `execution_max_snapshot_skew_ms`, permit **at most one** targeted **exact native-ID Matchbook get_market** refresh after *all required PM token book requests finish*, using the **same existing execution-candidate provider admission / shared scheduler**. Don't run broad event discovery, repeat PM token reads, reset queue deadlines, or add a retry loop. Avoid extra MB request on fast/synchronous calls or cases where PM itself is missing, inconsistent, stale across tokens or intrinsically over-skewed. MB market must remain the exact approved active market/event ID, not terminal/gone and not mismatched. If the tail refresh times out, is rate limited, fails, or yields stale source clocks, reject normally and report why.
- Replace **only** the candidate's Matchbook `RetrievedVenuePayload` in the authoritative in-memory `_ExecutionBookSet` with the new, verified snapshot; don't mix best price from old MB and price/depth from new MB. All PM constituent payloads and timestamps are preserved exactly. Entire final `paper_scan.scan_pair`, economics, fee/FX, accepted net edge, generalized payoff, native order freeze and rounding economics must recompute **from the final coherent book set**. Do not treat the original discovery result or prior snapshot as approved; no dispatch until the final complete accepted `ExecutionSnapshot`.
- Still enforce unchanged configured `paper_execution_max_snapshot_skew_ms` default **500ms**, `paper_entry_max_quote_age_ms` default **2000ms** inclusive of existing simulated latency, all per-book timestamps / completeness, native min-order and stakes, supported market/equivalence, execution/fill/treasury risk. If PM source or MB source timestamp is stale, second retrieval *must not* magically fix it by replacing source age with local network completion time. Reevaluate at the *later* finish. A slow second Matchbook call that ages PM beyond 2s or widens skew must fail. Limit may also fail if PM constituent token returns spread apart beyond threshold — do not try to paper over this with MB.
- Do NOT change `SPORTS_HEDGE_MODE=real` + `SPORTS_HEDGE_EXECUTION_ENABLED=false` launcher; `PAPER_AUTOFILL_ENABLED=false` unchanged; no actual order submission, credentials or geo changes, no scanner restarts/deploys. No percentage buffer/new profit floors. Keep operator economics: 0.20% minimum net, £3 combined GBP per tranche, £5 per opportunity, existing risk controls.
- Preserve the *single* Price-2 authority and original frozen-order package, snapshot id chain, idempotency and opening/top-up ordering. Apply to initial Price-2 and existing iterative reprices through shared `_reprice_exact_books`, not separate side path or shadow arbitrage solver. No extra child tasks leaking or HOT/BACKGROUND/UNIVERSE starvation. Do not block Python event loop.
- Audit initial vs tail retrival with clear `matchbook_tail_refresh_attempted`, `success/failed/skipped`, `original_skew_ms`, `final_skew_ms`, per-provider request timing, final freshness, and `Price2 accepted/rejected` reason. Reuse existing telemetry and keep live/paper reporting honest. Explain reason when original 5.83% disappears on refreshed MB, or is still too old, rather than claiming recovered arb. No noisy UI required for this branch.

## Acceptance tests

1. Initially MB retrieved t=0 and two PM books at t=2.68 and 2.72s; tail MB at t=2.78s; correct source clock, age, fees and FX => exact one additional MB GET; final skew within 500ms; fresh approved snapshot and native freeze; no venue orders with disabled execution. Use fake clocks and venue transports.
2. The new MB odds/depth are worse enough that an initial theoretical 5% arb vanishes => no Price-2 acceptance, no OPEN, no use of old limit/stake.
3. Second MB times out, returns wrong ID/terminal/empty, or 429 => reject, preserve audit evidence; do not refetch PM nor endlessly retry.
4. PM already stale at tail completion OR multiple PM tokens are >500ms apart OR MB exchange source timestamp is still stale => reject even if retrieval timestamps appear aligned.
5. No tail refresh when initial skew <= existing limit; no tail refresh for unsupported venue pair or when PM missing/incomplete; no loop when tail fails.
6. Race/concurrency: existing per-row duplicate suppression, permit one attempt, no task leak, no drift from execution scheduler priority/budget. Include both first opening Price-2 and iterated follow-up Price-2 tests.
7. Exact existing operator capital, native minimum, fresh FX/fee, stop/timeout and partial hedging semantics preserved; no new economic authority.
8. Run relevant targeted backend, Linux full, Windows full backend/liveness and launcher CI. Never loosen timing thresholds or skip flaky tests.

## Coordination

Parallel draft [PR #42](https://github.com/osmith94/Sports-Hedge-Real/pull/42) repairs STREAM fee/FX and redesigns the UI. Avoid overlapping code in STREAM, frontend and launcher; scope this wave to existing Price-2 `price_engine.py` and focused tests + audit. If both modify `price_engine.py`, coordinate a clean rebase/test before either merge. Do **not** merge either PR or enable execution. Follow `AGENTS.md`, `docs/CORE_TENETS.md` and applicable tenets. Update this PR with exact tests and limitations.

## Later follow-up, NOT authorized by this PR

A separate read-only validation could evaluate using *populated, authenticated-by-observation native Polymarket WS token books* as a faster future pricing source. Do NOT substitute STREAM shadow order books for the current live Price-2 authority without an independent integrity and settlement review. This wave is exclusively the bounded Matchbook tail refresh.
