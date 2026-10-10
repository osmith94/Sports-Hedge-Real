# Wave 37 — one Price-2 execution path (integration working contract)

**Status:** In progress, not production-ready. Integration against `main` for Issue #37.
**Source PRs:** #32 STREAM, #35 native-freeze diagnostics, #38 iterative live TOP_UP, #40 native depth sweep, #33 Market Maker documentation only.
**Operator restriction:** No main merge, auto-merge, deployment, Windows scanner restart, changes to launcher/runtime credentials, live execution arming, actual exchange orders, or market-maker order posting without later explicit approval. Keep `SPORTS_HEDGE_EXECUTION_ENABLED=false` throughout this wave.

## Single end-to-end path

1. HOT/BACKGROUND or one manually pinned STREAM fixture detects a meaningful qualifying observation. STREAM book deltas (real native PM token IDs) update the cached PM book; only significant moves, potential edge, depth changes, reconnect baseline or bounded reconciliation request a coalesced targeted Matchbook native-ID refresh, through shared provider admission. Avoid per-tick Matchbook requests or new scanner loops.
2. Both sources feed **the same existing authoritative fresh Price-2**, canonical market equivalence and settlement validation, venue-only fees, FX, executable native depth, operator configured minimum net edge, and native order sizing/freezing. **No parallel arb solver, fee logic, execution dispatcher, or paper-only STREAM destination**.
3. Price-2 uses the *actual bounded native orders* for acceptance. Matchbook BACK odds limit reaches the **worst admitted** price across relevant levels; Polymarket FAK BUY max-price reaches the worst admitted BUY level. Expected weighted blend is informative, never an execution guarantee. Exact native-ID Matchbook Price-2 depth may use `minimum-liquidity=0` and adequate levels, without bloating UNIVERSE.
4. Existing opening pathway chooses PAPER with execution disarmed and LIVE **only after a separate future operator arming**. A live opening can TOP_UP only after both legs are confirmed fully filled, reconciled, without resting/unknown/partial exposure; each further tranche gets a **new** accepted Price-2 and *genuinely marginal* fresh book/balances, appends distinct immutable attempt/order IDs, fills and audits. No discretionary TOP_UP when open exposure is unknown/partial; use existing reconciliation/recovery instead.
5. Preserve #35 per-leg PM/MB native constraint diagnostics in every opening and top-up snapshot, and the existing Activity projection. Historical rows lacking evidence say "details not recorded".

## Operator capital and profitability definitions (do not silently change)

- **Max One-Time** = maximum **combined GBP capital across every leg of one complete hedge tranche**, not per-venue order. It **resets for each independent iteration**; not a cumulative opportunity ceiling.
- **Max Opportunity** = maximum cumulative combined GBP deployed to one opportunity across its opening + all top-ups.
- **Max Event** = maximum cumulative combined GBP deployed across opportunities for the same event.
- Example: Max One-Time £3, Max Opportunity £5, Max Event £5 -> a confirmed £3 full hedge can be followed by a distinct £2 full hedge, subject to current permitted economics, native venue funds and actual marginal depth; then stop at £5.
- **Minimum Net Arb** = only the currently configured operator threshold applicable to the market, after the existing actual fees/FX/native quantities/rounding. **Do not invent** extra percentage buffers, profit minimums, risk-score reductions, arbitrary stake caps, mandatory trade-size multipliers, automatic resizing past limits, new maximum-concurrent-opportunity caps, or hidden haircut controls. Distinguish true venue native order minimum/tick and hedge-completeness rules from newly invented controls.
- Aim for the **largest genuinely executable, economically valid tranche** permitted by existing operator ceilings, venue money, depth and original threshold. Small absolute profit alone is not a veto.

## Required focused correctness fixes at integration

- **#40 exact frozen payoff:** `execution/depth_plan.py::qualifies_at_conservative_limits` currently calls `CompleteSetArbitrageSolver.solve`, which may re-optimise the stake distribution instead of evaluating the **exact intended submitted native quantities**. Verify final snapped per-leg prices, shares and GBP capital, and **minimum payout across actual relevant settlement states for the exact order pair** after venue fees and FX. A differently sized theoretical hedge is not proof for the submitted pair. Reuse existing settlement/payoff authorities rather than inventing another solver. Account for generalized payoff markets, not only simple complete-set.
- **#40 test weakness:** remove `or True` from the depth-sweep assertion in `backend/tests/test_issue39_native_depth_sweep.py` and replace it with a meaningful falsifiable assertion. Add adversarial test where alternative stake ratio is profitable but the exact frozen pair is not.
- **#40 level sweep:** demonstrate first MB level £0.50 at best, further £4.50 at worse level within a £5 leg, frozen native limit reaches second level, and exact complete hedge still satisfies the operator threshold; if worsening price breaks economics, downsize to genuinely valid size or reject. Preserve PM minimum shares/tick, MB ladder, FX, per-venue funds and partial/remainder handling.
- **#32 STREAM gap:** current shadow runtime explicitly does not call Price-2/opening; add a small handoff into existing Price-2/dispatch. Prove one **read-only** real-token subscribed populated PM CLOB book/delta => coalesced exact-ID MB refresh where feasible. Empty WS `[]` alone is not a passing end-to-end test. If live read-only verification cannot be performed, log the exact blocker; don't claim proven connectivity.
- **#38 iteration:** protect opening and each TOP_UP ledger identity, snapshot uniqueness, consumed depth and treasury reservations, idempotency on concurrent HOT/ACTIVE triggers or restart, and no submit while disarmed. Preserve PAPER accumulation behaviour.
- **#35 shared edits:** preserve both STREAM API status and Price-2 diagnostic types in `frontend/lib/api.ts`, diagnostics through `execution_snapshot.py` and `execution/frozen.py`, and the narrow Windows issue517 **test-only** ACTIVE TRADE race fix from #32. Avoid duplicative liveness patches.
- **Windows event-loop CI:** record baseline vs integrated performance for issue496/517/572 and GC attribution; don't bypass, skip, or silently relax actual timing limits. Shared GC pauses and scheduler races seen in standalone PRs should be diagnosed once on this branch.
- **Paper/native eligibility mismatch:** `mode=real` + execution OFF may reject paper on proof of native executable orders. Preserve current behaviour pending separate operator policy decision; do not conflate diagnostic-only #35 with a fix.

## Integrated acceptance tests — all on THIS head

1. One STREAM fixture: valid PM native-token book/delta -> significance trigger -> targeted coalesced MB exact-ID refresh -> **same Price-2** -> existing paper opening with execution OFF; no extra market-maker, new solver or dispatcher.
2. Ordinary HOT/BACKGROUND opportunities continue through identical Price-2 and opening logic; identical economics for the same bounded books.
3. Fake MB/PM multi-level books + venue fees + USD/GBP FX -> verified exact frozen native orders + payoff at every settlement state, including adverse rounding, native minima and shallow/better first levels.
4. Simulated *fully acknowledged live* opening £3 GBP combined -> new Price-2, consumed-depth-aware genuinely distinct £2 GBP combined TOP_UP -> stops at £5 cumulative event/opportunity ceilings. Execution OFF test must place **zero** real orders; enabled flags only in deterministic fake-venue test clients.
5. Partial MB/PM fills, RESTING MB remainder, stale/suspended order book, unknown venue ack, crash/restart, duplicate snapshot, recovery, provider timeout and stop -> **no further discretionary top-up**; immutable diagnostics and accounting preserved.
6. Provider saturation, PM WS reconnect/resync and Windows lane cadence/ACTIVE TRADE responsiveness under UNIVERSE load. No stream per-tick MB flood.
7. Full Linux + full Windows backend suite, Windows targeted liveness, frontend tests/build/typecheck and Windows launcher CI, all checked on integrated head. Highlight any red check, don't declare ready.

## Integration discipline

- Use this one fresh branch and one draft PR; import the source PRs with reviewed conflict resolution, not parallel edits on `main`. #33 is docs-only; no working Market Maker implementation.
- Add an **integrated evidence summary** to PR description/comments: exact head SHA, file changes, all CI links, synthetic native-wire test excerpts, remaining honest unknowns, and explicit no-order-when-OFF evidence.
- **Stop at draft PR review.** The owner separately decides whether to merge/install later, and whether to enable real execution in a distinct future step.
