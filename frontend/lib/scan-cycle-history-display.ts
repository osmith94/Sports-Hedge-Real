import { PaperScanCycleRecord } from "./api";
import { formatObservationAge } from "./observation-age";

export const SCAN_CYCLE_TITLE = "Scan cycle history";

export const SCAN_CYCLE_COPY =
  "Latest 100 completed HOT / UNIVERSE refresh cycles, newest first. One row per cycle, including cycles with zero paper decisions. Not market-decision audit.";

export const SCAN_CYCLE_EMPTY = "No completed scan cycles yet. Zero-decision cycles still appear here once a Fast Scan or Full Sweep finishes.";

export const SCAN_CYCLE_UNAVAILABLE =
  "Scan cycle history unavailable. No fabricated cycles.";

export const SCAN_CYCLE_HEADERS = [
  "Completed",
  "Lane",
  "Duration",
  "Fixtures",
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
  if (value === "hot") return "HOT";
  if (value === "universe") return "UNIVERSE";
  return lane ? String(lane).toUpperCase() : "—";
}

export function scanCycleDurationLabel(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  return `${Math.round(ms / 100) / 10}s`;
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
    fixtureLabel: String(cycle.fixture_count),
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

export function scanCycleBadgeLabel(
  available: boolean,
  cycles: PaperScanCycleRecord[] | null | undefined,
): string {
  if (!available) return "UNAVAILABLE";
  const count = cycles?.length ?? 0;
  return count ? `${Math.min(count, 100)} CYCLES` : "EMPTY";
}
