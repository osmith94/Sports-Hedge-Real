import { PaperScanCycleRecord, ScanCycleDiagnosticReport } from "./api";
import { formatObservationAge } from "./observation-age";

export const MAIN_SCAN_CYCLE_LIMIT = 8;

export const SCAN_CYCLE_TITLE = "Scan cycle history";
export const SCAN_CYCLE_RECENT_TITLE = "Recent scan cycles";
export const SCAN_CYCLE_DIAGNOSTICS_TITLE = "Diagnostics / Scan history";

export const SCAN_CYCLE_COPY =
  "Latest 50 completed HOT pricing / BACKGROUND pricing / UNIVERSE discovery cycles, newest first. One row per cycle, including cycles with zero paper decisions. Loaded from the scan-cycle endpoint, not the live heartbeat. HOT and BACKGROUND counts are catalogue/market rows. UNIVERSE counts are fixtures. Unique fixture totals are shown only when that number is stored. Not market-decision audit.";

export const SCAN_CYCLE_EMPTY = "No completed scan cycles yet. Zero-decision cycles still appear here once HOT pricing, BACKGROUND pricing or UNIVERSE discovery finishes.";

export const SCAN_CYCLE_UNAVAILABLE =
  "Scan cycle history unavailable. No fabricated cycles.";

export const SCAN_CYCLE_HEADERS = [
  "Completed",
  "Lane",
  "Duration",
  "Coverage",
  "Evaluated",
  "Matched",
  "Paper decisions",
  "Qualifying arbs",
  "Health",
] as const;

export type ScanCycleRow = {
  id: string;
  laneLabel: string;
  completedLabel: string;
  durationLabel: string;
  fixtureLabel: string;
  evaluatedLabel: string;
  matchedLabel: string;
  paperDecisionLabel: string;
  qualifyingLabel: string;
  healthLabel: string;
  degraded: boolean;
};

export function scanCycleLaneLabel(lane: string | null | undefined): string {
  const value = String(lane || "").trim().toLowerCase();
  if (value === "hot") return "HOT pricing";
  if (value === "background") return "BACKGROUND pricing";
  if (value === "universe") return "UNIVERSE discovery";
  return lane ? String(lane).toUpperCase() : "—";
}

export function scanCycleDurationLabel(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  return `${Math.round(ms / 100) / 10}s`;
}

export function scanCycleCoverageLabel(cycle: PaperScanCycleRecord): string {
  const count = Number(cycle.fixture_count);
  const n = Number.isFinite(count) ? count : 0;
  const lane = String(cycle.scan_lane || "").trim().toLowerCase();
  const summary = String(cycle.operator_summary || "").trim();
  if (lane === "hot" && summary.includes("HOT fixtures")) {
    return summary;
  }
  if (lane === "hot" || lane === "background") {
    return n === 1 ? "1 catalogue row" : `${n} catalogue rows`;
  }
  if (lane === "universe") {
    return n === 1 ? "1 fixture" : `${n} fixtures`;
  }
  return String(n);
}

export function scanCycleHealthLabel(cycle: PaperScanCycleRecord): string {
  if (cycle.last_error) {
    const error = String(cycle.last_error);
    if (/timeout|list_events|429/i.test(error)) return `provider failure · ${error}`;
    return `scanner error · ${error}`;
  }
  const health = cycle.venue_health ?? {};
  const failed = Object.entries(health)
    .filter(([, value]) => value === "unavailable" || value === "timeout" || value === "degraded" || value === "error" || value === "failed")
    .map(([venue, value]) => `${venue} ${value}`);
  if (failed.length) return failed.join(" · ");
  if ((cycle.not_evaluated_count ?? 0) > 0) {
    return `partial · ${cycle.not_evaluated_count} not evaluated`;
  }
  const skippedNote = String(cycle.operator_summary || "");
  if (/unsupported markets skipped/i.test(skippedNote)) {
    return "unsupported markets skipped";
  }
  if ((cycle.matched_event_pairs ?? 0) > 0 && (cycle.matched_market_pairs ?? 0) === 0) {
    return "evaluated · 0 equivalent markets";
  }
  if (cycle.degraded) return "degraded";
  const ok = Object.keys(health).length ? "venues ok" : "health unknown";
  return ok;
}

export function scanCycleCompletedLabel(
  cycle: PaperScanCycleRecord,
  nowMs: number | null = null,
): string {
  if (!cycle.completed_at) return "—";
  if (nowMs == null || !Number.isFinite(nowMs)) return cycle.completed_at;
  return `${formatObservationAge(cycle.completed_at, nowMs)} ago`;
}

export function scanCycleRow(
  cycle: PaperScanCycleRecord,
  nowMs: number | null = null,
): ScanCycleRow {
  return {
    id: cycle.cycle_id,
    laneLabel: scanCycleLaneLabel(cycle.scan_lane),
    completedLabel: scanCycleCompletedLabel(cycle, nowMs),
    durationLabel: scanCycleDurationLabel(cycle.duration_ms),
    fixtureLabel: scanCycleCoverageLabel(cycle),
    evaluatedLabel: `${cycle.evaluated_count} / ${cycle.not_evaluated_count} leftover`,
    matchedLabel: `${cycle.matched_event_pairs} evt · ${cycle.matched_market_pairs} mkt`,
    paperDecisionLabel: String(cycle.paper_decision_count),
    qualifyingLabel: String(cycle.qualifying_arb_count),
    healthLabel: scanCycleHealthLabel(cycle),
    degraded: Boolean(cycle.degraded),
  };
}

export function scanCycleRows(
  cycles: PaperScanCycleRecord[] | null | undefined,
  nowMs: number | null = null,
): ScanCycleRow[] {
  return (cycles ?? []).map((cycle) => scanCycleRow(cycle, nowMs));
}

export function recentScanCycleRows(
  cycles: PaperScanCycleRecord[] | null | undefined,
  nowMs: number | null = null,
  limit = MAIN_SCAN_CYCLE_LIMIT,
): ScanCycleRow[] {
  return scanCycleRows(cycles, nowMs).slice(0, limit);
}

export function scanCycleBadgeLabel(
  available: boolean,
  cycles: PaperScanCycleRecord[] | null | undefined,
): string {
  if (!available) return "UNAVAILABLE";
  const count = cycles?.length ?? 0;
  return count ? `${Math.min(count, 50)} CYCLES` : "EMPTY";
}

export const SCAN_CYCLE_REPORT_ACTION = "View report";

export const SCAN_CYCLE_REPORT_EMPTY =
  "No stored diagnostic for this cycle. The short summary above is unchanged. Nothing was fabricated.";

export function scanCycleDiagnosticLines(report: ScanCycleDiagnosticReport | null | undefined): string[] {
  if (!report) return [];
  const terminals = report.terminals ?? {};
  const shape = report.call_shape ?? {};
  const limits = shape.provider_limits ?? {};
  const limitText = Object.entries(limits)
    .map(([venue, value]) => `${venue} ${value}`)
    .join(", ");
  const hotCoverage = report.hot_coverage;
  const lines = [
    report.note || "Cycle diagnostic counters. Not live quotes.",
    `Lane ${report.lane || "—"} · wall ${scanCycleDurationLabel(report.wall_ms)} · ${report.evaluations_per_second ?? 0} evaluated/s`,
    `Due ${report.due ?? 0} · considered ${report.considered ?? 0} · decisions ${report.decisions ?? 0} · qualifying ${report.qualifying ?? 0} · promoted HOT ${report.promoted_hot ?? 0}`,
    `Evaluated ${terminals.evaluated ?? 0} · skipped ${terminals.skipped ?? 0} · revalidation ${terminals.revalidation ?? 0} · failed ${terminals.failed ?? 0}`,
    `Retry wait ${terminals.retry_wait ?? 0} · capacity deferred ${terminals.deferred ?? 0} · not started ${terminals.not_started ?? 0} · collapsed leftover ${report.leftover_collapsed ?? 0}`,
    `Provider I/O sum ${report.provider_io_ms_sum ?? 0}ms · slot wait sum ${report.slot_wait_ms_sum ?? 0}ms · local evaluate sum ${report.local_evaluate_ms_sum ?? 0}ms`,
    `Saved calls ${report.saved_provider_calls ?? 0} · coalesced ${report.coalesced_provider_calls ?? 0} · repeated exact ids ${report.repeated_exact_id_calls ?? 0} / distinct ${report.distinct_exact_ids ?? 0}`,
    `Workers ${shape.worker_limit ?? "—"} · sequential within item ${shape.sequential_within_item ? "yes" : "no"} · explicit slice wall ${shape.explicit_slice_wall ? "yes" : "no"} · limits ${limitText || "—"} · provider calls ${shape.provider_calls ?? 0}`,
  ];
  if (hotCoverage && typeof hotCoverage.summary === "string" && hotCoverage.summary) {
    lines.push(hotCoverage.summary);
  }
  for (const stage of report.stages ?? []) {
    lines.push(
      `${stage.venue || "stage"} ${stage.stage || "—"} · n ${stage.count ?? 0} · avg ${stage.avg_ms ?? 0}ms · p50 ${stage.p50_ms ?? "—"} · p95 ${stage.p95_ms ?? "—"} · max ${stage.max_ms ?? "—"} · ok ${stage.success ?? 0} · timeout ${stage.timeout ?? 0} · rate limit ${stage.rate_limit ?? 0} · deferred ${stage.capacity_deferred ?? 0}`,
    );
  }
  for (const slow of report.slowest ?? []) {
    lines.push(
      `Slow ${String(slow.venue || "")} ${String(slow.stage || "")} ${String(slow.elapsed_ms || 0)}ms ${String(slow.outcome || "")} ${String(slow.source_id || "")}`.trim(),
    );
  }
  for (const error of report.worker_errors ?? []) {
    lines.push(`Worker error ${error.type || "error"} · ${error.message || ""}`.trim());
  }
  const samples = report.samples ?? {};
  for (const [bucket, rows] of Object.entries(samples)) {
    for (const row of rows ?? []) {
      lines.push(`${bucket} ${row.row_id || "—"} · ${row.reason || ""}`.trim());
    }
  }
  if (report.timing_note) lines.push(report.timing_note);
  return lines;
}

export function scanCycleLatestSummary(
  available: boolean,
  cycles: PaperScanCycleRecord[] | null | undefined,
): string {
  if (!available) return "unavailable";
  const latest = cycles?.[0];
  if (!latest) return "no completed cycles";
  return `latest ${scanCycleLaneLabel(latest.scan_lane)} · ${scanCycleHealthLabel(latest)}`;
}
