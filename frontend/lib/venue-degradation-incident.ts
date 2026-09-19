import type { LiveRefreshStatus, VenueDegradationIncident } from "./api";
import { isUiDegradedHealth } from "./venue-health-display";

export const VENUE_DEGRADATION_INCIDENT_SCHEMA = "sports_hedge.venue_degradation_incident.v1";
export const VENUE_DEGRADATION_INCIDENT_KIND = "in_memory_transition_snapshot";
export const VENUE_DEGRADATION_FALLBACK_KIND = "already_polled_live_refresh_read_model";
export const MAX_VENUE_DEGRADATION_INCIDENTS = 8;
export const FIRST_CLASS_DEGRADATION_VENUES = ["matchbook", "polymarket", "kalshi"] as const;

const LOCAL_BACKPRESSURE = new Set([
  "waiting",
  "deferred",
  "provider_capacity_saturated",
]);
const PROVIDER_TIMEOUT = new Set([
  "timeout",
  "discovery_timeout",
  "market_timeout",
]);
const AUTH_UNAVAILABLE = new Set(["auth_failure", "unavailable"]);

export type VenueDegradationIncidentStore = {
  previousHealth: Record<string, string | undefined>;
  latest: Record<string, VenueDegradationIncident>;
  retained: VenueDegradationIncident[];
};

export function createVenueDegradationIncidentStore(): VenueDegradationIncidentStore {
  return { previousHealth: {}, latest: {}, retained: [] };
}

export function venueDegradationFilename(venue: string, capturedAt: string): string {
  const stamp = String(capturedAt || "unknown")
    .replace(/[:.]/g, "-")
    .replace(/[^\dTZ-]/gi, "");
  return `sports-hedge-${venue}-degradation-${stamp}.json`;
}

function laneHealth(
  status: LiveRefreshStatus | null | undefined,
  lane: "hot" | "background" | "universe",
  venue: string,
): string | undefined {
  return status?.[lane]?.venue_health?.[venue];
}

function operationValues(
  status: LiveRefreshStatus | null | undefined,
  lane: "hot" | "background" | "universe",
  venue: string,
): string[] {
  return recordValues(status?.[lane]?.operation_health?.[venue]);
}

function recordValues(value: unknown): string[] {
  if (!value || typeof value !== "object") return [];
  return Object.values(value as Record<string, unknown>).map((item) => String(item));
}

function classifyVenueDegradation(
  status: LiveRefreshStatus,
  venue: string,
): Record<string, unknown> {
  const hotHealth = laneHealth(status, "hot", venue);
  const backgroundHealth = laneHealth(status, "background", venue);
  const universeHealth = laneHealth(status, "universe", venue);
  const top = status.venue_health?.[venue];
  const hotOps = [
    ...operationValues(status, "hot", venue),
    ...recordValues(status.price_engine?.hot?.operation_health?.[venue]),
  ];
  const universeOps = operationValues(status, "universe", venue);
  const backgroundOps = [
    ...operationValues(status, "background", venue),
    ...recordValues(status.price_engine?.background?.operation_health?.[venue]),
  ];
  const allOps = [...hotOps, ...universeOps, ...backgroundOps];
  const laneValues = [hotHealth, backgroundHealth, universeHealth, top];
  const waiting = Number((status.provider_access?.waiting as Record<string, number> | undefined)?.[venue] ?? 0);
  const waitingByLane = (status.provider_access?.waiting_by_lane ?? {}) as Record<
    string,
    Record<string, number>
  >;
  let laneWaiting = 0;
  for (const counts of Object.values(waitingByLane)) {
    laneWaiting += Number(counts?.[venue] ?? 0);
  }
  const backgroundDeferred = Number(status.price_engine?.background?.deferred ?? 0);
  const backgroundSaturated = Number(
    status.price_engine?.background?.provider_capacity_saturated ?? 0,
  );
  return {
    hot_health: hotHealth,
    background_health: backgroundHealth,
    universe_health: universeHealth,
    top_level_health: top,
    hot_ok: hotHealth === "ok",
    hot_market_timeout: hotHealth === "market_timeout" || hotOps.includes("market_timeout"),
    universe_discovery_timeout:
      universeHealth === "discovery_timeout" || universeOps.includes("discovery_timeout"),
    provider_timeout:
      allOps.some((value) => PROVIDER_TIMEOUT.has(value)) ||
      laneValues.some((value) => value !== undefined && PROVIDER_TIMEOUT.has(value)),
    auth_or_unavailable:
      allOps.some((value) => AUTH_UNAVAILABLE.has(value)) ||
      laneValues.some((value) => value !== undefined && AUTH_UNAVAILABLE.has(value)),
    local_backpressure: Boolean(
      waiting ||
        laneWaiting ||
        backgroundDeferred ||
        backgroundSaturated ||
        allOps.some((value) => LOCAL_BACKPRESSURE.has(value)) ||
        laneValues.some((value) => value !== undefined && LOCAL_BACKPRESSURE.has(value)),
    ),
    retry_wait_painting:
      laneValues.some((value) => value === "retry_wait") || allOps.includes("retry_wait"),
    mixed_lane_degraded: hotHealth === "ok" && isUiDegradedHealth(universeHealth),
  };
}

function laneSnapshot(lane: LiveRefreshStatus["hot"]): Record<string, unknown> {
  if (!lane) return {};
  return {
    venue_health: lane.venue_health ?? {},
    operation_health: lane.operation_health ?? {},
    last_error: lane.last_error ?? null,
    last_started_at: lane.last_started_at ?? null,
    last_completed_at: lane.last_completed_at ?? null,
    last_duration_ms: lane.last_duration_ms ?? null,
    last_heartbeat_at: lane.last_heartbeat_at ?? null,
    last_plan_reason: lane.last_plan_reason ?? null,
    last_diagnostics: lane.last_diagnostics ?? null,
    worker_state: lane.worker_state ?? null,
    degraded: lane.degraded ?? false,
    canonical_retryable: lane.canonical_retryable,
    series_retryable: lane.series_retryable,
    evaluated_count: lane.evaluated_count,
    not_evaluated_count: lane.not_evaluated_count,
    operator_summary: lane.operator_summary ?? null,
  };
}

function cycleSummaries(status: LiveRefreshStatus): Array<Record<string, unknown>> {
  return (status.recent_scan_cycles ?? []).slice(0, 10).map((cycle) => ({
    cycle_id: cycle.cycle_id,
    started_at: cycle.started_at,
    completed_at: cycle.completed_at,
    scan_lane: cycle.scan_lane,
    duration_ms: cycle.duration_ms,
    fixture_count: cycle.fixture_count,
    evaluated_count: cycle.evaluated_count,
    not_evaluated_count: cycle.not_evaluated_count,
    venue_health: cycle.venue_health ?? {},
    degraded: cycle.degraded ?? false,
    last_error: cycle.last_error ?? null,
    operator_summary: cycle.operator_summary ?? null,
    universe_generation_id: cycle.universe_generation_id ?? null,
    resume_cursor: cycle.resume_cursor ?? null,
    completeness: cycle.completeness ?? null,
    generation_resume: cycle.generation_resume ?? null,
    generation_work_used_s: cycle.generation_work_used_s ?? null,
  }));
}

export function buildVenueDegradationIncident(
  status: LiveRefreshStatus,
  venue: string,
  options: {
    previousHealth?: string | null;
    newHealth?: string | null;
    capturedAt?: string;
    dataKind?: string;
  } = {},
): VenueDegradationIncident {
  const capturedAt = options.capturedAt ?? new Date().toISOString();
  const newHealth = options.newHealth ?? status.venue_health?.[venue] ?? null;
  const previousHealth = options.previousHealth ?? null;
  return {
    schema: VENUE_DEGRADATION_INCIDENT_SCHEMA,
    data_kind: options.dataKind ?? VENUE_DEGRADATION_INCIDENT_KIND,
    captured_at: capturedAt,
    affected_venue: venue,
    transition: {
      previous_health: previousHealth,
      new_health: newHealth,
      reason: `${previousHealth ?? "unknown"}->${newHealth ?? "unknown"}`,
    },
    venue_health: { ...(status.venue_health ?? {}) },
    hot: laneSnapshot(status.hot),
    background: laneSnapshot(status.background),
    universe: laneSnapshot(status.universe),
    price_engine: {
      hot: {
        venue_health: status.price_engine?.hot?.venue_health ?? {},
        operation_health: status.price_engine?.hot?.operation_health ?? {},
        last_error: status.price_engine?.hot?.last_error ?? null,
        retry_wait: status.price_engine?.hot?.retry_wait,
        deferred: status.price_engine?.hot?.deferred,
        provider_capacity_saturated: status.price_engine?.hot?.provider_capacity_saturated,
        working_set: status.price_engine?.hot?.working_set,
      },
      background: {
        venue_health: status.price_engine?.background?.venue_health ?? {},
        operation_health: status.price_engine?.background?.operation_health ?? {},
        last_error: status.price_engine?.background?.last_error ?? null,
        retry_wait: status.price_engine?.background?.retry_wait,
        deferred: status.price_engine?.background?.deferred,
        provider_capacity_saturated: status.price_engine?.background?.provider_capacity_saturated,
        working_set: status.price_engine?.background?.working_set,
      },
    },
    provider_access: {
      inflight: status.provider_access?.inflight ?? {},
      waiting: status.provider_access?.waiting ?? {},
      waiting_by_lane: status.provider_access?.waiting_by_lane ?? {},
      limits: status.provider_access?.limits ?? {},
    },
    recent_scan_cycles: cycleSummaries(status),
    active_catalogue_count:
      Number(status.price_engine?.hot?.working_set ?? 0) +
      Number(status.price_engine?.background?.working_set ?? 0),
    classification: classifyVenueDegradation(status, venue),
  };
}

export function fallbackVenueDegradationIncident(
  status: LiveRefreshStatus,
  venue: string,
  capturedAt = new Date().toISOString(),
): VenueDegradationIncident {
  return buildVenueDegradationIncident(status, venue, {
    previousHealth: null,
    newHealth: status.venue_health?.[venue] ?? null,
    capturedAt,
    dataKind: VENUE_DEGRADATION_FALLBACK_KIND,
  });
}

export function observeVenueDegradationIncidents(
  store: VenueDegradationIncidentStore,
  status: LiveRefreshStatus,
  capturedAt = new Date().toISOString(),
): Record<string, VenueDegradationIncident> {
  const backend = status.venue_degradation_incidents;
  const top = status.venue_health ?? {};
  for (const venue of FIRST_CLASS_DEGRADATION_VENUES) {
    const current = top[venue];
    const previous = store.previousHealth[venue];
    const backendIncident = backend?.[venue];
    if (backendIncident && isUiDegradedHealth(current)) {
      store.latest[venue] = backendIncident;
    } else if (isUiDegradedHealth(current) && !isUiDegradedHealth(previous) && !backendIncident) {
      const incident = buildVenueDegradationIncident(status, venue, {
        previousHealth: previous ?? null,
        newHealth: current,
        capturedAt,
      });
      store.latest[venue] = incident;
      store.retained.push(incident);
      if (store.retained.length > MAX_VENUE_DEGRADATION_INCIDENTS) {
        store.retained.splice(0, store.retained.length - MAX_VENUE_DEGRADATION_INCIDENTS);
      }
    } else if (!isUiDegradedHealth(current)) {
      delete store.latest[venue];
    }
    store.previousHealth[venue] = current;
  }
  const visible: Record<string, VenueDegradationIncident> = {};
  for (const venue of FIRST_CLASS_DEGRADATION_VENUES) {
    if (isUiDegradedHealth(top[venue]) && store.latest[venue]) {
      visible[venue] = store.latest[venue];
    }
  }
  return visible;
}

export function resolveVenueWhyIncident(
  venue: string,
  incidents: Record<string, VenueDegradationIncident> | undefined,
  status: LiveRefreshStatus | null,
  capturedAt = new Date().toISOString(),
): VenueDegradationIncident | null {
  if (incidents?.[venue]) return incidents[venue];
  if (!status) return null;
  return fallbackVenueDegradationIncident(status, venue, capturedAt);
}

export type JsonDownloadBridge = {
  createObjectURL?: (blob: Blob) => string;
  revokeObjectURL?: (url: string) => void;
  click?: (anchor: { href: string; download: string }) => void;
};

export function downloadVenueWhyIncident(
  incident: VenueDegradationIncident,
  bridge: JsonDownloadBridge = {},
): { filename: string; href: string; json: string } {
  const filename = venueDegradationFilename(incident.affected_venue, incident.captured_at);
  const json = `${JSON.stringify(incident, null, 2)}\n`;
  const blob = new Blob([json], { type: "application/json" });
  const createObjectURL =
    bridge.createObjectURL ??
    (typeof URL !== "undefined" && typeof URL.createObjectURL === "function"
      ? (value: Blob) => URL.createObjectURL(value)
      : undefined);
  const href = createObjectURL ? createObjectURL(blob) : `data:application/json;charset=utf-8,${encodeURIComponent(json)}`;
  const click =
    bridge.click ??
    ((anchor) => {
      if (typeof document === "undefined") return;
      const node = document.createElement("a");
      node.href = anchor.href;
      node.download = anchor.download;
      node.rel = "noopener";
      document.body.appendChild(node);
      node.click();
      node.remove();
    });
  click({ href, download: filename });
  const revoke =
    bridge.revokeObjectURL ??
    (typeof URL !== "undefined" && typeof URL.revokeObjectURL === "function"
      ? (value: string) => URL.revokeObjectURL(value)
      : undefined);
  if (revoke && href.startsWith("blob:")) revoke(href);
  return { filename, href, json };
}
