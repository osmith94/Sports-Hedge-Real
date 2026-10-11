import { describe, expect, it } from "vitest";

import type { Price2ActivityObservation } from "./api";
import { summarizePrice2Attempts, timedSummary } from "./price2-performance";

function attempt(
  observation_id: string,
  overrides: Partial<Price2ActivityObservation> = {},
): Price2ActivityObservation {
  return {
    observation_id,
    opportunity_id: "watch:sample",
    source: "execution_snapshot_audit",
    occurred_at: "2026-10-11T18:00:00Z",
    status: "rejected",
    filled: false,
    data_kind: "historical_recorded",
    legs: [],
    ...overrides,
  };
}

describe("Price-2 performance: recorded bounded sample", () => {
  it("computes p50/p90 only from valid recorded timings, including zero", () => {
    expect(timedSummary([null, 0, 20, 100, -1, Number.NaN, 500])).toEqual({
      count: 4, p50: 20, p90: 500,
    });
    expect(timedSummary([])).toEqual({ count: 0, p50: null, p90: null });
  });

  it("distinguishes accepted price checks from actual fills and deduplicates stable IDs", () => {
    const rows = summarizePrice2Attempts([
      attempt("accepted", { status: "accepted", accepted: true, filled: false, elapsed_ms: 900,
        oldest_quote_age_ms: 100, skew_ms: 50 }),
      attempt("accepted", { status: "accepted", accepted: true, filled: true, elapsed_ms: 100 }),
      attempt("rejected", { rejection_reason: "execution_reprice_skew", elapsed_ms: 2900,
        oldest_quote_age_ms: null, skew_ms: 1800 }),
      attempt("unavailable", { source: "lifecycle_rejection", status: "incomplete_unavailable",
        rejection_reason: "execution_reprice_failed" }),
    ]);
    expect(rows.attempts).toBe(3);
    expect([rows.accepted, rows.rejected, rows.unavailable]).toEqual([1, 1, 1]);
    expect(rows.acceptedNotFilled).toBe(1);
    expect(rows.recordedFills).toBe(0);
    expect(rows.evaluation).toEqual({ count: 2, p50: 900, p90: 2900 });
    expect(rows.quoteAge).toEqual({ count: 1, p50: 100, p90: 100 });
    expect(rows.rejectionReasons).toEqual([
      { reason: "execution_reprice_failed", count: 1 },
      { reason: "execution_reprice_skew", count: 1 },
    ]);
  });

  it("keeps venue-aggregated max timing separate by provider, not summed or imputed", () => {
    const rows = summarizePrice2Attempts([
      attempt("one", { venue_timings: [
        { venue: "matchbook", slot_wait_ms: 12, io_ms: 90, call_count: 1 },
        { venue: "polymarket", slot_wait_ms: 22, io_ms: 1200, call_count: 2 },
        { venue: "account", slot_wait_ms: 0, io_ms: 400 },
      ] }),
      attempt("two", { venue_timings: [
        { venue: "polymarket", slot_wait_ms: null, io_ms: 300, call_count: 2 },
      ] }),
    ]);
    expect(rows.venues).toEqual([
      { venue: "matchbook", attempts: 1,
        wait: { count: 1, p50: 12, p90: 12 },
        io: { count: 1, p50: 90, p90: 90 } },
      { venue: "polymarket", attempts: 2,
        wait: { count: 1, p50: 22, p90: 22 },
        io: { count: 2, p50: 300, p90: 1200 } },
    ]);
  });

  it("reports safe Polymarket freeze subreasons without treating older unknowns as evidence", () => {
    const summary = summarizePrice2Attempts([
      attempt("depth", { legs: [
        { venue: "polymarket", freeze_status: "not_frozen",
          freeze_reason: "approved_depth_incompatible" },
        { venue: "polymarket", freeze_status: "frozen", freeze_reason: "frozen" },
        { venue: "matchbook", freeze_status: "not_frozen",
          freeze_reason: "native_order_translation_other" },
      ] }),
      attempt("legacy", { legs: [
        { venue: "polymarket", freeze_status: "details_not_recorded",
          freeze_reason: "details_not_recorded" },
      ] }),
      attempt("invalid", { legs: [
        { venue: "polymarket", freeze_status: "not_frozen",
          freeze_reason: "native_order_translation_invalid_input" },
      ] }),
    ]);
    expect(summary.polymarketFreezeReasons).toEqual([
      { reason: "approved_depth_incompatible", count: 1 },
      { reason: "native_order_translation_invalid_input", count: 1 },
    ]);
  });

  it("empty audits show unknown timing, not fabricated zero-speed performance", () => {
    const data = summarizePrice2Attempts([]);
    expect(data.attempts).toBe(0);
    expect(data.evaluation.p50).toBeNull();
    expect(data.venues).toEqual([]);
    expect(data.rejectionReasons).toEqual([]);
  });
});
