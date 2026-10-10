# Wave 38 — STREAM pricing repair and compact operator panel

> **Operator limit correction (2026-10-10):** The operator has confirmed that Max Event / Max Opportunity / Max One-Time are existing persisted dashboard settings (shown as £7/£7/£5 in a PAPER-mode screenshot), NOT hard-coded canary limits. Any earlier reference in this spec to £3/£5 or fixed £5 event/opportunity test ceilings is superseded. This wave must use actual effective operator settings from `effective_operator_scanner_settings(settings)` and existing `policy_from_settings`, never override, add a separate session cap, or modify runtime deployment controls. This wave is not authorised to turn on live execution. See updated [Wave 40 contract](WAVE40_LIVE_CANARY.md) on PR #44's branch.


## Purpose and operator evidence

After merging PR #41 (main merge `8d20bbb`), the operator observed active Polymarket market-WS subscriptions and coalesced Matchbook exact-ID requests, but zero trustworthy STREAM comparisons / Price-2 attempts for a pinned Real Madrid v Villarreal event. `GET /stream/status` while paused retained nine diagnostic rows with *the same* reasons:
- `unknown_required_venue_cost:polymarket`
- `missing_venue_cost:matchbook`, `unknown_costs`
- `missing_fx_rate:USD`
- `stream_shadow_not_price2`
The panel is also very large, verbose, visually cluttered, and lacks per-market operational explanations. Address all of this as ONE narrow corrective wave, without refactoring HOT/BACKGROUND pricing or broadening live capabilities.

## Proven code paths (verify against current main before editing)

1. `application/stream/shadow.py` uses `_isolated_paper_scan()` which instantiates `PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository()))` without normal FX / fee / liquidity / mapping services. `api/paper.py::scheduled_paper_scan_service` constructs a properly wired scanner; `price_engine.py::_scan_kwargs`, `_polymarket_fee_snapshot_payload`, and `_build_polymarket_obs` show the normal source authority.
2. `StreamShadowComparer.compare` builds a PM observation without the persisted `identity.polymarket_fee_snapshot_id` fee snapshot and uses `quote_age_reason="stream_shadow_not_price2"`. `PaperScanService.scan_pair` treats every `quote_age_reason` as a rejection. Shadow/telemetry classification MUST NOT contaminate rejection reasons. Preserve real freshness errors as errors.
3. `application/stream/runtime.py` instantiates this comparer by default and shows only opaque aggregate counters. `frontend/components/stream-panel.tsx` has two repetitive headers, oversized explanatory copy and an inline word-wall of counters; `frontend/app/globals.css` contains only minimal STREAM-specific styling.
4. The real launcher sets `SPORTS_HEDGE_MODE=real`, `SPORTS_HEDGE_EXECUTION_ENABLED=false`, `PAPER_AUTOFILL_ENABLED=false`. Do not change these, or authorize venue order submissions. No capital/threshold/policy loosening.

## Backend acceptance

- Make STREAM comparisons reuse exactly the EXISTING authoritative venue fee resolver/account fee store, Polymarket persisted per-market fee snapshot (including native fee evidence), FX service and current USD/GBP published rate, catalogue identity and operator min-net, risk/limits, and existing market matcher/solver. Prefer injecting/reusing the existing configured scanner/dependencies, without creating new pricing or execution authorities; bound CPU/SQLite overhead on high-frequency events. Do not fetch FX/fees/accounts on every token tick. Reuse the Price-2 engine's snapshot and market data source authorities where appropriate, while respecting no duplicate provider reads.
- Do not invent a fee or a USD rate. Missing/expired metadata must still fail closed with a precise reason. No venue-specific sport branches, fee assumptions, FX fallbacks, hardcoded rates or removal of required gate.
- `stream_shadow_not_price2` is a `data_class`/display classification only and MUST NOT be inserted into `quote_age_reason` or the scanner's economic rejections. Real malformed/unknown/future/stale clock must remain rejected. Clearly separate **data integrity**, **net opportunity/economic qualification**, and **Price-2 accepted / recorded opening**. Never mark a diagnostic shadow quote as execution-authoritative.
- Use the existing `StreamPrice2Handoff` and authoritative `capture_with_execution_reprice`; prove with an end-to-end fake-native-book + coalesced exact MB test that a fully valid profitable candidate reaches Price-2 at least once with `SPORTS_HEDGE_EXECUTION_ENABLED=false` and submits ZERO real orders. Missing fee, missing FX, stale or mismatched settlement tests must fail before Price-2. Ensure unprofitable but valid comparisons remain visible as valid/no arb, not defective.
- The UI/status should not imply PAUSED `0/19` books are healthy or count cached candidates as current. Preserve counters as historical/session-scoped or explicitly label them; avoid historical Price-2 success badges on a fresh stale market.
- Observe integration that `PM` REST is slow: separate existing queue and rate-limit / skew behavior; don't silently relax the 500 ms skew, 2,000 ms paper-entry quote-age, or configured 0.20% net minimum. Do not override persisted Max Event / Max Opportunity / Max One-Time (`effective_operator_scanner_settings` / `policy_from_settings`), invent a session budget, or add a first-trade / one-opportunity restriction. Generalized payoff guardrails stay as they are. A separate follow-up may optimize sync; not part of the acceptance unless plainly safe.

## Compact frontend operator design

Make STREAM visually integrated with the existing dark green/charcoal console, with a restrained emerald accent, existing design tokens and small clean typography. Goal: *at first glance*: fixture, status, market-data health, opportunity count, Price-2 attempts/accepts, **trades actually opened**, and what is blocking. Not an engineering paragraph. Build a compact dedicated `.stream-panel` component and responsive CSS, not inline styles or an external design system.

- A SINGLE top bar: small `STREAM` heading + prominently readable fixture (competition optional), clear states `SUBSCRIBED / CONNECTING / DEGRADED / STALE / PAUSED / NOT SELECTED` and an unmistakable **EXECUTION OFF** indicator, plus correctly enabled **Pause / Resume / Remove** actions. Don't imply live execution can be toggled from this panel.
- A short metrics ribbon (e.g. `PM books 0/19`, `MB quotes 0/9`, `Valid pairs 0`, `Potential arbs 0`, `Price-2 attempts 0 / accepted 0`, `Paper fills 0`). Clarify token vs market count, and don't call an active subscription a populated book.
- A small table/list for pinned market keys, **data / economics / Price-2** states separately, net edge only if actually computed, pair age / skew in human-readable units, and concise *top specific reason* (e.g. missing PM fee, FX unavailable, MB quote stale), with expandable full reasons. Show 9 markets elegantly; no giant list in a paragraph.
- Move reconnect count, bad/unknown messages, rate limits, coalesced/suppressed/dropped counts, last trigger, provider timestamps, details and technical limitations into collapsed `Details / Diagnostics`; preserve access to all telemetry and rejection reasons. Accessible semantic `details/summary`, proper focus, button states, narrow-viewport wrapping and text-contrast.
- Do not mislabel `price2` flag as an entered trade. No fake counters, non-grounded timestamps, hard-coded success. The status response may need additive typed fields to separate completeness/data health vs pricing viability; no breaking `GET /stream/status` contract for existing clients. Keep status API provider-read-free.
- Update `frontend/lib/stream-status-display.ts`, its tests, `frontend/components/stream-panel.tsx`, `frontend/app/globals.css`, and relevant frontend API types. Add component-level tests for paused/no fixture, valid but non-arb, missing costs, Price-2 accepted but no fill, responsive/collapsed diagnostics behavior, and stale book label. Remove the unwieldy repeated engineering copy; optionally put static help behind disclosure.

## Validation and evidence

- Backend targeted tests of scanner shared dependencies, native PM fee/FX, no phantom quote-age rejection, stream-to-P2 handoff, zero execution orders while disabled; retain existing tests for STREAM session/cadence/reconnect. Be specific about whether FX/fee evidence actually present. Do not change fixture semantics to make tests pass.
- Linux backend and Windows full/liveness CI, frontend lint/build/test, Windows launcher if relevant. Do not weaken timing assertions, skip CI, or reintroduce blocking network requests in status GET / hot-event callback.
- Show a concise before/after *text-only* operator screenshot or test snapshot in PR and state precisely what can and cannot be proven without live credentials/data.
- Log / audit for shadow -> candidate -> attempted -> Price-2 rejected/accepted -> opening attempted/not attempted -> trade OPEN in an accurate and non-duplicating way.
- No merge, deploy, scanner restart, credentials changes, execution enablement, or real order submission without separate explicit operator consent. Remain draft for review.

## Completion reporting

Comment on PR with changed file list, main design decisions, targeted tests and all CI links, sample status demonstrating a candidate no longer rejected merely for missing injected FX/fee/quote_age_reason, and clear unresolved blockers.