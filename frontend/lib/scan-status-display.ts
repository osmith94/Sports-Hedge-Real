import { LiveRefreshStatus } from "./api";
import { formatObservationAge } from "./observation-age";
import { lastScanVenueClause } from "./venue-participation-display";

export type LaneScanCopy = {
  label: string;
  detail: string;
};

function durationLabel(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return `${Math.round(ms / 100) / 10}s`;
}

function completedClock(iso: string | null | undefined, now?: number | null): string {
  if (!iso) return "never";
  if (now == null || !Number.isFinite(now)) return `completed at ${iso}`;
  return `completed ${formatObservationAge(iso, now)} ago`;
}

function nextDueClock(iso: string | null | undefined, now?: number | null): string {
  if (!iso) return "next due —";
  if (now == null || !Number.isFinite(now)) return `next due ${iso}`;
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "next due —";
  const delta = Math.max(0, Math.round((then - now) / 1000));
  return `next due in ${delta}s`;
}

export function fastScanCopy(
  status: LiveRefreshStatus | null,
  now: number | null = null,
): LaneScanCopy {
  const hot = status?.hot;
  if (!hot) {
    return { label: "Fast scan", detail: "never" };
  }
  const venues = lastScanVenueClause(status, "hot");
  const venueSuffix = venues ? ` · ${venues}` : "";
  if (hot.cycle_in_progress) {
    return { label: "Fast scan", detail: `in progress${venueSuffix}` };
  }
  const leftover = hot.not_evaluated_count
    ? ` · partial (${hot.not_evaluated_count} not evaluated)`
    : "";
  const persist =
    hot.persist_ok === false || hot.last_persist_error
      ? " · persist/auto-capture failed"
      : "";
  return {
    label: "Fast scan",
    detail: `${completedClock(hot.last_completed_at, now)} · ran ${durationLabel(hot.last_duration_ms)} · ${nextDueClock(hot.next_due_at, now)} · ${hot.fixture_count} hot${venueSuffix}${leftover}${persist}`,
  };
}

export function fullSweepCopy(
  status: LiveRefreshStatus | null,
  now: number | null = null,
): LaneScanCopy {
  const universe = status?.universe;
  const hot = status?.hot;
  if (!universe) {
    return { label: "Full sweep", detail: "never" };
  }
  const venues = lastScanVenueClause(status, "universe");
  const venueSuffix = venues ? ` · ${venues}` : "";
  if (universe.cycle_in_progress) {
    return { label: "Full sweep", detail: `chunk in progress${venueSuffix}` };
  }
  const work = universe.generation_work_used_s ?? 0;
  const budget = universe.generation_budget_seconds ?? 150;
  const leftover = universe.not_evaluated_count ?? 0;
  const evaluated = universe.evaluated_count ?? 0;
  const persist =
    universe.persist_ok === false || universe.last_persist_error
      ? " · persist/auto-capture failed"
      : "";
  return {
    label: "Full sweep",
    detail: `chunk ran ${durationLabel(universe.chunk_last_duration_ms ?? universe.last_duration_ms)} · gen ${Math.round(work)}/${Math.round(budget)}s · HOT ${nextDueClock(hot?.next_due_at, now)} · ${universe.fixture_count} universe · ${evaluated} evaluated / ${leftover} not evaluated${venueSuffix}${persist}`,
  };
}

export function dualScanStatusLines(
  status: LiveRefreshStatus | null,
  now: number | null = null,
): string[] {
  const fast = fastScanCopy(status, now);
  const full = fullSweepCopy(status, now);
  return [`${fast.label} · ${fast.detail}`, `${full.label} · ${full.detail}`];
}
