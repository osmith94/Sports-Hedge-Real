# Price-2 performance and Polymarket freeze diagnostics

## Scope and safety

This is a **read-only dashboard and diagnostics** wave. It does not change quote collection, execution thresholds (quote age, skew, minimum edge), native stake/tick/rounding arithmetic, venue order dispatch, duplicate protection, risk checks, real execution ON/OFF, or scanner cadence.

## Dashboard

The home page shows a **Price-2 performance** panel after scan-cycle history. It reads the existing indexed `GET /paper/watchlist/price2-attempts` endpoint using `since=<24 hours ago>&include_recent=true&limit=50`, with a request started concurrently with existing home-page data. There is **no browser polling** and no calls to trading venues. The backend has an existing 50-record recent cap, so the panel is explicitly the *latest 50 within 24 hours* and not a full 24-hour census.

Shown, for the same bounded sample:
- recorded acceptance, rejection and unavailable counts (acceptance is **never** called a fill);
- evaluation time, retrieval skew, oldest quote age: p50/p90 among rows with *recorded* nonnegative finite milliseconds and explicit sample counts;
- per-venue p50/p90 of **venue-aggregated max** provider slot wait and I/O timings, with coverage counts. These are not per-leg timings, additive time spent, end-to-end submission latency or guaranteed time saved;
- top recorded Price-2 rejection reasons and Polymarket native order freeze failure codes;
- missing-telemetry coverage, sample cap and recorded fill flags. Historical/unavailable rows do not invent provider timing.

Refresh the home page to see new records. The activity feed still shows individual Price-2 snapshots and native-order legs.

## Polymarket freeze failure codes

Price-2 freeze now distinguishes:
- `approved_depth_incompatible`: the approved depth sweep cannot cover the stake / approved levels;
- `native_order_translation_invalid_input`: invalid local decimal/type input;
- `native_order_translation_unexpected_error`: unexpected missing property during translation;
- `price_rounding_incompatible`: accepted depth price cannot be represented at the native tick;
- `unsupported_native_side`: unsupported native action;
- existing stable reasons, including `below_native_minimum_shares`, `unsupported_tick_size`, `stake_rounding_or_size_incompatible`, `missing_or_invalid_native_market_or_token`, remain unchanged.

Legacy `native_order_translation_other` remains readable and is the **fail-closed fallback** for uncoded TranslationErrors. New subcodes describe the exception category only. Raw exception text, stack traces, credentials and provider response payloads are never included in the audit. All failure paths continue to refuse an order exactly as before. Historical records cannot be retroactively classified more precisely.

Tests cover bounded sample summaries/coverage, duplicate IDs, never counting acceptance as a fill, stable reason propagation to existing activity projection, and failure-code mapping with no private error text.

## Next, only after inspecting the recorded breakdown

Identify whether the dominant Price-2 bottleneck is venue slot wait, Matchbook I/O, Polymarket I/O, or timestamp/source-age validation. Optimize that specific cause in a **separate** code change with deterministic native-book tests. No blanket increase in venue concurrency, no relaxed quote-age/skew gate, and no substitution of unverified stream books for authoritative Price-2.
