import type { Price2ActivityObservation } from "./api";

// A read-only sample of the existing bounded Price-2 audit projection.
// Never infer a fill from Price-2 acceptance or impute missing timing data.
export type TimedSummary = { count: number; p50: number | null; p90: number | null };
export type VenuePerformance = {
  venue: string;
  attempts: number;
  wait: TimedSummary;
  io: TimedSummary;
};
export type ReasonCount = { reason: string; count: number };

export type Price2PerformanceSummary = {
  attempts: number;
  accepted: number;
  rejected: number;
  unavailable: number;
  acceptedNotFilled: number;
  recordedFills: number;
  evaluation: TimedSummary;
  skew: TimedSummary;
  quoteAge: TimedSummary;
  venues: VenuePerformance[];
  rejectionReasons: ReasonCount[];
  polymarketFreezeReasons: ReasonCount[];
};

function numeric(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

export function timedSummary(values: readonly (number | null | undefined)[]): TimedSummary {
  const observed = values.filter(numeric).sort((a, b) => a - b);
  const nearest = (fraction: number): number | null =>
    observed.length ? observed[Math.max(0, Math.ceil(fraction * observed.length) - 1)] : null;
  return { count: observed.length, p50: nearest(0.5), p90: nearest(0.9) };
}

function orderedCounts(values: readonly string[]): ReasonCount[] {
  const counts = new Map<string, number>();
  for (const reason of values) counts.set(reason, (counts.get(reason) ?? 0) + 1);
  return [...counts].map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => b.count - a.count || a.reason.localeCompare(b.reason));
}

export function summarizePrice2Attempts(attempts: readonly Price2ActivityObservation[]): Price2PerformanceSummary {
  const unique = new Map<string, Price2ActivityObservation>();
  for (const attempt of attempts) {
    if (!attempt.observation_id || unique.has(attempt.observation_id)) continue;
    unique.set(attempt.observation_id, attempt);
  }
  const rows = [...unique.values()];
  const venues = new Map<string, {
    attempts: number; wait: (number | null | undefined)[]; io: (number | null | undefined)[];
  }>();
  for (const item of rows) {
    // Venue timing is already a max across all calls in one attempt, not a
    // sum and not a per-leg metric. It must never be added across venues.
    const seen = new Set<string>();
    for (const timing of item.venue_timings ?? []) {
      const venue = timing.venue?.toLowerCase();
      if (!venue || venue === "account" || seen.has(venue)) continue;
      seen.add(venue);
      const target = venues.get(venue) ?? { attempts: 0, wait: [], io: [] };
      target.attempts += 1;
      target.wait.push(timing.slot_wait_ms);
      target.io.push(timing.io_ms);
      venues.set(venue, target);
    }
  }

  return {
    attempts: rows.length,
    accepted: rows.filter((row) => row.status === "accepted").length,
    rejected: rows.filter((row) => row.status === "rejected").length,
    unavailable: rows.filter((row) => row.status === "incomplete_unavailable").length,
    acceptedNotFilled: rows.filter((row) => row.status === "accepted" && !row.filled).length,
    recordedFills: rows.filter((row) => row.filled).length,
    evaluation: timedSummary(rows.map((row) => row.elapsed_ms)),
    skew: timedSummary(rows.map((row) => row.skew_ms)),
    quoteAge: timedSummary(rows.map((row) => row.oldest_quote_age_ms)),
    venues: [...venues].map(([venue, data]) => ({
      venue, attempts: data.attempts, wait: timedSummary(data.wait), io: timedSummary(data.io),
    })).sort((a, b) => a.venue.localeCompare(b.venue)),
    rejectionReasons: orderedCounts(rows
      .filter((row) => row.status !== "accepted")
      .map((row) => row.rejection_reason?.trim() || "reason_not_recorded")),
    polymarketFreezeReasons: orderedCounts(rows.flatMap((row) => (row.legs ?? [])
      .filter((leg) => leg.venue?.toLowerCase() === "polymarket" && leg.freeze_status === "not_frozen")
      .map((leg) => leg.freeze_reason?.trim() || "reason_not_recorded"))),
  };
}
