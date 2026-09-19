import { LiveRefreshStatus, SystemLoadSummary } from "./api";

export type SystemLoadLine = {
  key: string;
  detail: string;
};

type ProviderAccessMaps = {
  inflight?: Record<string, unknown>;
  waiting?: Record<string, unknown>;
  limits?: Record<string, unknown>;
};

const DEFAULT_PROVIDER_LIMITS: Record<string, number> = {
  matchbook: 4,
  kalshi: 4,
};

export function cadenceUtilisation(
  lastDurationMs: number | null | undefined,
  cadenceSeconds: number | null | undefined,
): number | null {
  if (cadenceSeconds == null || !(cadenceSeconds > 0)) return null;
  if (lastDurationMs == null || !Number.isFinite(lastDurationMs) || lastDurationMs < 0) {
    return null;
  }
  const ratio = lastDurationMs / (cadenceSeconds * 1000);
  return Number.isFinite(ratio) ? ratio : null;
}

export function deriveSystemLoad(status: LiveRefreshStatus | null | undefined): SystemLoadSummary {
  const hot = status?.hot;
  const universe = status?.universe;
  const hotEngine = status?.price_engine?.hot;
  const backgroundEngine = status?.price_engine?.background;
  const access = (status?.provider_access ?? {}) as ProviderAccessMaps;
  const hotWorking = asCount(hotEngine?.working_set);
  const backgroundWorking = asCount(backgroundEngine?.working_set);
  const lastCycleMs = optionalCount(hot?.last_duration_ms);
  const cadenceSeconds = asCount(hot?.cadence_seconds);
  const evaluated = firstCount(universe?.canonical_evaluated, universe?.evaluated_count);
  const total = firstCount(
    universe?.canonical_work_total,
    universe?.discovered_total,
    universe?.fixture_count,
  );
  const remaining =
    universe?.canonical_remaining ?? universe?.remaining ?? Math.max(0, total - evaluated);
  return {
    hot: {
      fixtures: asCount(hot?.fixture_count),
      working_set: hotWorking,
      due: asCount(hotEngine?.due),
      in_flight: asCount(hotEngine?.in_flight),
      retry_wait: asCount(hotEngine?.retry_wait),
      deferred: asCount(hotEngine?.deferred),
      last_cycle_ms: lastCycleMs,
      cadence_seconds: cadenceSeconds,
      cadence_utilisation: cadenceUtilisation(lastCycleMs, cadenceSeconds),
    },
    matchbook: providerSlot(access, "matchbook"),
    kalshi: providerSlot(access, "kalshi"),
    universe: {
      evaluated,
      total,
      remaining: asCount(remaining),
      generation_work_used_s: optionalFinite(universe?.generation_work_used_s),
      generation_budget_seconds: optionalFinite(universe?.generation_budget_seconds),
    },
    catalogue_items: hotWorking + backgroundWorking,
  };
}

export function resolveSystemLoad(status: LiveRefreshStatus | null | undefined): SystemLoadSummary {
  if (status?.system_load) return status.system_load;
  return deriveSystemLoad(status);
}

export function systemLoadLines(status: LiveRefreshStatus | null | undefined): SystemLoadLine[] {
  const load = resolveSystemLoad(status);
  const hot = load.hot ?? {};
  const mb = load.matchbook ?? {};
  const kalshi = load.kalshi ?? {};
  const universe = load.universe ?? {};
  const cadence = asCount(hot.cadence_seconds);
  const cycle = formatCycle(hot.last_cycle_ms, cadence, hot.cadence_utilisation);
  const uniProgress = `${asCount(universe.evaluated)}/${asCount(universe.total)} evaluated`;
  const uniBudget = formatBudget(
    universe.generation_work_used_s,
    universe.generation_budget_seconds,
  );
  return [
    {
      key: "HOT",
      detail: `${asCount(hot.fixtures)} fixtures · ${asCount(hot.working_set)} items · ${asCount(hot.due)} due · ${cycle}`,
    },
    {
      key: "MB",
      detail: `${asCount(mb.inflight)}/${asCount(mb.limit)} in use · queue ${asCount(mb.waiting)}`,
    },
    {
      key: "K",
      detail: `${asCount(kalshi.inflight)}/${asCount(kalshi.limit)} in use · queue ${asCount(kalshi.waiting)}`,
    },
    {
      key: "UNI",
      detail: uniBudget ? `${uniProgress} · ${uniBudget}` : uniProgress,
    },
    {
      key: "ALL",
      detail: `${asCount(load.catalogue_items)} catalogue items`,
    },
  ];
}

function formatCycle(
  durationMs: number | null | undefined,
  cadenceSeconds: number,
  utilisation: number | null | undefined,
): string {
  const duration = durationMs == null ? "—" : `${Math.round(durationMs / 100) / 10}s`;
  const cadence = cadenceSeconds > 0 ? `${cadenceSeconds}s` : "—";
  const ratio =
    utilisation == null || !Number.isFinite(utilisation)
      ? cadenceUtilisation(durationMs, cadenceSeconds)
      : utilisation;
  if (ratio == null) return `cycle ${duration} / ${cadence}`;
  return `cycle ${duration} / ${cadence} (${Math.round(ratio * 100)}%)`;
}

function formatBudget(used: number | null | undefined, budget: number | null | undefined): string | null {
  if (used == null && budget == null) return null;
  const usedLabel = used == null || !Number.isFinite(used) ? "—" : `${Math.round(used)}s`;
  const budgetLabel = budget == null || !Number.isFinite(budget) ? "—" : `${Math.round(budget)}s`;
  return `${usedLabel} / ${budgetLabel}`;
}

function providerSlot(access: ProviderAccessMaps, venue: string) {
  const limits = access.limits ?? {};
  const defaultLimit = DEFAULT_PROVIDER_LIMITS[venue] ?? 0;
  return {
    inflight: asCount(access.inflight?.[venue]),
    waiting: asCount(access.waiting?.[venue]),
    limit: venue in limits ? asCount(limits[venue]) : defaultLimit,
  };
}

function asCount(value: unknown): number {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return 0;
  return Math.trunc(number);
}

function firstCount(...values: unknown[]): number {
  for (const value of values) {
    if (value == null) continue;
    return asCount(value);
  }
  return 0;
}

function optionalCount(value: unknown): number | null {
  if (value == null) return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return null;
  return Math.trunc(number);
}

function optionalFinite(value: unknown): number | null {
  if (value == null) return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return null;
  return number;
}
