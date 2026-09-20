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
  if (hot.last_plan_reason === "hot_scope_empty" && hot.last_heartbeat_at) {
    return {
      label: "Fast scan",
      detail: `worker alive · scope empty · polling · no provider call${venueSuffix}${persist}`,
    };
  }
  if (!hot.last_completed_at && !hot.last_started_at && !hot.last_heartbeat_at) {
    return { label: "Fast scan", detail: "never" };
  }
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
  if (!universe) {
    return { label: "Full sweep", detail: "never" };
  }
  const venues = lastScanVenueClause(status, "universe");
  const venueSuffix = venues ? ` · ${venues}` : "";
  const leftover = universe.not_evaluated_count ?? 0;
  const evaluated = universe.canonical_evaluated ?? universe.evaluated_count ?? 0;
  const discovered = universe.canonical_work_total ?? universe.discovered_total ?? 0;
  const remaining = universe.canonical_remaining ?? universe.remaining ?? leftover;
  const persist =
    universe.persist_ok === false || universe.last_persist_error
      ? " · persist/auto-capture failed"
      : "";
  if (universe.cycle_in_progress || universe.worker_state === "running") {
    const progress =
      discovered > 0 ? `${evaluated}/${discovered} evaluated` : `${evaluated} evaluated`;
    return {
      label: "Full sweep",
      detail: `in progress · ${progress}${venueSuffix}${persist}`,
    };
  }
  const elapsed = durationLabel(universe.chunk_last_duration_ms ?? universe.last_duration_ms);
  const state = universe.worker_state && universe.worker_state !== "idle" ? ` · ${universe.worker_state}` : "";
  const cadence = universe.cadence_seconds ? ` · cadence ${universe.cadence_seconds}s` : "";
  const due = universe.next_due_at ? ` · ${nextDueClock(universe.next_due_at, now)}` : "";
  return {
    label: "Full sweep",
    detail: `elapsed ${elapsed}${state} · ${universe.fixture_count} universe · ${evaluated} evaluated / ${remaining} not evaluated${due}${cadence}${venueSuffix}${persist}`,
  };
}

export function dualScanStatusLines(
  status: LiveRefreshStatus | null,
  now: number | null = null,
): string[] {
  if (status?.scanner_stopped) {
    const lines = [
      "Fast scan · stopped by operator · no provider call",
      "Full sweep · stopped by operator · no provider call",
    ];
    if (status.background || status.price_engine?.background) {
      lines.push("Background price engine · stopped by operator · no provider call");
    }
    return lines;
  }
  const fast = fastScanCopy(status, now);
  const full = fullSweepCopy(status, now);
  const lines = [`${fast.label} · ${fast.detail}`, `${full.label} · ${full.detail}`];
  const background = backgroundPriceCopy(status, now);
  if (background) {
    lines.push(`${background.label} · ${background.detail}`);
  }
  return lines;
}

export function backgroundPriceCopy(
  status: LiveRefreshStatus | null,
  _now: number | null = null,
): LaneScanCopy | null {
  const engine = status?.price_engine?.background;
  const lane = status?.background;
  if (!engine && !lane) return null;
  const evaluated = engine?.evaluated ?? lane?.evaluated_count ?? 0;
  const retry = engine?.retry_wait ?? 0;
  const deferred = engine?.deferred ?? engine?.provider_capacity_saturated ?? 0;
  const notStarted = engine?.not_started_this_cadence ?? lane?.not_evaluated_count ?? 0;
  const working = engine?.working_set ?? 0;
  const inFlight = engine?.in_flight ?? 0;
  const suffix = lane?.cycle_in_progress ? " · in progress" : "";
  const cadence = lane?.cadence_seconds ? ` · cadence ${lane.cadence_seconds}s` : "";
  const due = lane?.next_due_at ? ` · ${nextDueClock(lane.next_due_at, _now)}` : "";
  return {
    label: "Background price engine",
    detail: `${working} ACTIVE · ${evaluated} evaluated · ${inFlight} in flight · ${retry} retry · ${deferred} deferred · ${notStarted} not started${cadence}${due}${suffix}`,
  };
}
