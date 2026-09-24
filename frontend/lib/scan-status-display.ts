import { LiveRefreshStatus } from "./api";
import { formatObservationAge } from "./observation-age";
import { lastScanVenueClause } from "./venue-participation-display";

export const HOT_PRICING_LABEL = "HOT pricing";
export const BACKGROUND_PRICING_LABEL = "BACKGROUND pricing";
export const UNIVERSE_DISCOVERY_LABEL = "UNIVERSE discovery";

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

function nextClock(
  iso: string | null | undefined,
  now: number | null | undefined,
  label: "next due" | "next scan" | "next discovery",
): string {
  if (!iso) return `${label} —`;
  if (now == null || !Number.isFinite(now)) return `${label} ${iso}`;
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return `${label} —`;
  const delta = Math.max(0, Math.round((then - now) / 1000));
  return `${label} in ${delta}s`;
}

export function hotPricingCopy(
  status: LiveRefreshStatus | null,
  now: number | null = null,
): LaneScanCopy {
  const hot = status?.hot;
  if (!hot) {
    return { label: HOT_PRICING_LABEL, detail: "never" };
  }
  const venues = lastScanVenueClause(status, "hot");
  const venueSuffix = venues ? ` · ${venues}` : "";
  if (hot.cycle_in_progress) {
    return { label: HOT_PRICING_LABEL, detail: `in progress${venueSuffix}` };
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
      label: HOT_PRICING_LABEL,
      detail: `worker alive · scope empty · polling · no provider call${venueSuffix}${persist}`,
    };
  }
  if (!hot.last_completed_at && !hot.last_started_at && !hot.last_heartbeat_at) {
    return { label: HOT_PRICING_LABEL, detail: "never" };
  }
  return {
    label: HOT_PRICING_LABEL,
    detail: `${completedClock(hot.last_completed_at, now)} · ran ${durationLabel(hot.last_duration_ms)} · ${nextClock(hot.next_due_at, now, "next scan")} · ${hot.fixture_count} hot${venueSuffix}${leftover}${persist}`,
  };
}

/** @deprecated Use hotPricingCopy. Kept as a compatibility alias. */
export const fastScanCopy = hotPricingCopy;

export function universeDiscoveryCopy(
  status: LiveRefreshStatus | null,
  now: number | null = null,
): LaneScanCopy {
  const universe = status?.universe;
  if (!universe) {
    return { label: UNIVERSE_DISCOVERY_LABEL, detail: "never" };
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
      label: UNIVERSE_DISCOVERY_LABEL,
      detail: `in progress · ${progress}${venueSuffix}${persist}`,
    };
  }
  const refreshSeconds = universe.discovery_refresh_seconds ?? universe.cadence_seconds;
  const cadence = refreshSeconds ? ` · discovery refresh ${refreshSeconds}s` : "";
  if (
    status?.universe_scans_paused &&
    (universe.last_plan_reason === "universe_scheduled_paused" || universe.next_due_at == null)
  ) {
    return {
      label: UNIVERSE_DISCOVERY_LABEL,
      detail: `Paused${cadence}${venueSuffix}${persist}`,
    };
  }
  const elapsed = durationLabel(universe.chunk_last_duration_ms ?? universe.last_duration_ms);
  const state = universe.worker_state && universe.worker_state !== "idle" ? ` · ${universe.worker_state}` : "";
  const due = universe.next_due_at ? ` · ${nextClock(universe.next_due_at, now, "next discovery")}` : "";
  return {
    label: UNIVERSE_DISCOVERY_LABEL,
    detail: `elapsed ${elapsed}${state} · ${universe.fixture_count} universe · ${evaluated} evaluated / ${remaining} not evaluated${due}${cadence}${venueSuffix}${persist}`,
  };
}

/** @deprecated Use universeDiscoveryCopy. Kept as a compatibility alias. */
export const fullSweepCopy = universeDiscoveryCopy;

export function dualScanStatusLines(
  status: LiveRefreshStatus | null,
  now: number | null = null,
): string[] {
  if (status?.scanner_stopped) {
    return [
      "ACTIVE TRADE · stopped by operator · no provider call",
      `${HOT_PRICING_LABEL} · stopped by operator · no provider call`,
      `${BACKGROUND_PRICING_LABEL} · stopped by operator · no provider call`,
      `${UNIVERSE_DISCOVERY_LABEL} · stopped by operator · no provider call`,
    ];
  }
  const active = activeTradeCopy(status, now);
  const hot = hotPricingCopy(status, now);
  const background = backgroundPriceCopy(status, now);
  const universe = universeDiscoveryCopy(status, now);
  return [
    `${active.label} · ${active.detail}`,
    `${hot.label} · ${hot.detail}`,
    `${background.label} · ${background.detail}`,
    `${universe.label} · ${universe.detail}`,
  ];
}

export function activeTradeCopy(
  status: LiveRefreshStatus | null,
  now: number | null = null,
): LaneScanCopy {
  const lane = status?.active_trade;
  if (!lane) {
    return { label: "ACTIVE TRADE", detail: "none" };
  }
  if (lane.cycle_in_progress) {
    return { label: "ACTIVE TRADE", detail: "in progress · exact-ID 5s" };
  }
  const open = lane.fixture_count ?? 0;
  const overdue = lane.not_evaluated_count ?? 0;
  const overdueBit = overdue > 0 ? ` · ${overdue} overdue` : "";
  return {
    label: "ACTIVE TRADE",
    detail: `${open} open · exact-ID 5s${overdueBit} · ${nextClock(lane.next_due_at, now, "next due")}`,
  };
}

export function backgroundPriceCopy(
  status: LiveRefreshStatus | null,
  now: number | null = null,
): LaneScanCopy {
  const engine = status?.price_engine?.background;
  const lane = status?.background;
  if (!engine && !lane) {
    return { label: BACKGROUND_PRICING_LABEL, detail: "never" };
  }
  const evaluated = engine?.evaluated ?? lane?.evaluated_count ?? 0;
  const retry = engine?.retry_wait ?? 0;
  const deferred = engine?.deferred ?? engine?.provider_capacity_saturated ?? 0;
  const notStarted = engine?.not_started_this_cadence ?? lane?.not_evaluated_count ?? 0;
  const working = engine?.working_set ?? 0;
  const inFlight = engine?.in_flight ?? 0;
  const suffix = lane?.cycle_in_progress ? " · in progress" : "";
  const scanSeconds = lane?.scan_interval_seconds ?? lane?.cadence_seconds;
  const repriceSeconds = lane?.reprice_after_seconds;
  const cadence = scanSeconds ? ` · scan interval ${scanSeconds}s` : "";
  const reprice = repriceSeconds ? ` · reprice after ${repriceSeconds}s` : "";
  const due = nextClock(lane?.next_due_at, now, "next scan");
  return {
    label: BACKGROUND_PRICING_LABEL,
    detail: `${working} ACTIVE · ${evaluated} evaluated · ${inFlight} in flight · ${retry} retry · ${deferred} deferred · ${notStarted} not started${cadence}${reprice} · ${due}${suffix}`,
  };
}
