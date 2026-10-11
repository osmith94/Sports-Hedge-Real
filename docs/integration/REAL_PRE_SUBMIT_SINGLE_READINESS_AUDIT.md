# Real execution: one pre-submit decision and durable refusal trail

## Operator intent

The single **EXECUTION ON / OFF** switch remains independent of venue preflight. One existing UI confirmation arms immediately; OFF blocks new orders and does not cancel exchange orders. No new user-facing capital caps, trading modes, geoblock bypass or manual confirmation steps.

## What changed

1. The scanner collects current-IP geoblock, authenticated venue-account/balance/allowance and unresolved-order evidence **before** obtaining the second fresh execution quote (Price-2). This is performed only when Real execution is armed. The 30-second read-only snapshot is shared using single-flight locking to avoid duplicate remote work across concurrent qualifying opportunities. A failed probe is cached as an explicit *denial*, never permission.
2. The pre-submit path uses the existing current (or explicitly injected test) facts **without remote reads**. Expired/missing evidence fails closed as `canary_preflight_not_collected`; a prior probe failure fails as `canary_preflight_unavailable`. The same existing `canary_submit_refusal` makes the final eligibility/operator-cap decision through `dispatch.refusal_reason`. Native IDs, frozen Price-2 order payload, matching venue clients, quote freshness, no duplicate package, and remaining venue constraints still gate the submission. The cheap local check is repeated inside `run_live_opening` after reservation to catch state changes; it does not repeat provider requests.
3. If live capture is armed and fails before the durable package reservation, the existing watchlist fill attempt is explicitly rejected with a durable `paper_fill_rejected` lifecycle event containing `live_pre_submit:<reason>`. This includes missing plan, missing transport, geoblock/allowance denial, accepted-snapshot validation and final quote expiry. No fake fill/trade or reserved live execution is created. If a package was reserved but the subsequent dispatch refuses, the attempt remains completed with its existing durable refusal and the watchlist also records the refusal. Unknown venue ACK/partial exposure behaviour is unchanged.
4. A duplicate already-reserved package is still suppressed without changing the active attempt or submitting a second order; the attempt itself is the durable record. No source changes to STREAM, HOT/BACKGROUND scheduling, fees/FX calculations, native venue dispatch or the user-facing execution switch.

## Deterministic tests

The Real canary test suite checks fail-closed blocked geoblock and stale accepted Price-2 cases have an exact persisted rejection, zero native orders and no live attempt, verifies cached readiness never calls provider collection after Price-2, and checks 30-second single-flight reuse and failure-denial behaviour. Existing positive native-order, duplicate/restart, frozen package, and real execution tests must pass. Real venue credentials/orders are not used by CI.

## Production note

The exact 17:17:44 missed order's original refusal is unknowable because the previous implementation returned before recording the reason. This PR will make **future** misses diagnosable. Real venue permission and native order requirements still cannot be bypassed; with unproven location/account eligibility, EXECUTION may display ON while the individual order is refused and reason-coded.

The frontend-only GTA audio work is separate draft PR #47, not included here.
