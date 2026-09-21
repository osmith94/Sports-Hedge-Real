import { SystemLoadSummary } from "./api";

export type SystemLoadLine = {
  key: string;
  detail: string;
};

export function systemLoadLines(
  load: SystemLoadSummary | null | undefined,
): SystemLoadLine[] {
  if (!load) {
    return [{ key: "—", detail: "unavailable" }];
  }
  const hot = load.hot ?? {};
  const background = load.background ?? {};
  const mb = load.matchbook ?? {};
  const kalshi = load.kalshi ?? {};
  const universe = load.universe ?? {};
  const active = load.active_trade ?? {};
  const overdue = asCount(active.overdue);
  const overdueBit = overdue > 0 ? ` · ${overdue} overdue` : "";
  const uniProgress = `${asCount(universe.evaluated)}/${asCount(universe.total)} evaluated`;
  const uniBudget = formatBudget(
    universe.generation_work_used_s,
    universe.generation_budget_seconds,
  );
  const selectedCount = asCount(universe.selected_competition_count);
  const generationScope = universe.generation_scope_version;
  const scopeBits = [
    selectedCount > 0 ? `${selectedCount} selected` : null,
    generationScope != null && Number.isFinite(generationScope)
      ? `gen v${Math.trunc(Number(generationScope))}`
      : null,
  ].filter(Boolean);
  return [
    {
      key: "ACTIVE TRADE",
      detail: `${asCount(active.open_trades)} open · ${asCount(active.due)} due${overdueBit} · cadence ${formatCadence(active.cadence_seconds, "5s")}`,
    },
    {
      key: "HOT pricing",
      detail: `${asCount(hot.fixtures)} fixtures · ${asCount(hot.working_set)} items · ${asCount(hot.due)} due · ${formatCycle(hot)}`,
    },
    {
      key: "BACKGROUND pricing",
      detail: `${asCount(background.working_set)} items · ${asCount(background.due)} due · cadence ${formatCadence(background.cadence_seconds)}`,
    },
    {
      key: "UNIVERSE discovery",
      detail: [
        uniProgress,
        `cadence ${formatCadence(universe.cadence_seconds)}`,
        uniBudget,
        ...scopeBits,
      ]
        .filter(Boolean)
        .join(" · "),
    },
    {
      key: "MB",
      detail: providerDetail(mb),
    },
    {
      key: "K",
      detail: providerDetail(kalshi),
    },
    {
      key: "ALL",
      detail: `${asCount(load.catalogue_items)} catalogue items`,
    },
  ];
}

function formatCycle(hot: NonNullable<SystemLoadSummary["hot"]>): string {
  const durationMs = hot.last_cycle_ms;
  const cadenceSeconds = asCount(hot.cadence_seconds);
  const duration = durationMs == null ? "—" : `${Math.round(durationMs / 100) / 10}s`;
  const cadence = cadenceSeconds > 0 ? `${cadenceSeconds}s` : "—";
  const ratio = hot.cadence_utilisation;
  if (ratio == null || !Number.isFinite(ratio)) return `cycle ${duration} / ${cadence}`;
  return `cycle ${duration} / ${cadence} (${Math.round(ratio * 100)}%)`;
}

function formatCadence(seconds: unknown, fallback = "—"): string {
  const cadence = asCount(seconds);
  return cadence > 0 ? `${cadence}s` : fallback;
}

function providerDetail(slot: NonNullable<SystemLoadSummary["matchbook"]>): string {
  const bits = [
    `${asCount(slot?.inflight)}/${asCount(slot?.limit)} in use`,
    `queue ${asCount(slot?.waiting)}`,
  ];
  const waitMs = asCount(slot?.wait_ms);
  if (waitMs > 0) bits.push(`wait ${formatMs(waitMs)}`);
  const latencyMs = asCount(slot?.latency_ms);
  if (latencyMs > 0) bits.push(`svc ${formatMs(latencyMs)}`);
  const misses = asCount(slot?.deadline_misses);
  if (misses > 0) bits.push(`${misses} deadline miss${misses === 1 ? "" : "es"}`);
  if (slot?.saturated) bits.push("saturated");
  return bits.join(" · ");
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${Math.round(ms / 100) / 10}s`;
}

function formatBudget(used: number | null | undefined, budget: number | null | undefined): string | null {
  if (used == null && budget == null) return null;
  const usedLabel = used == null || !Number.isFinite(used) ? "—" : `${Math.round(used)}s`;
  const budgetLabel = budget == null || !Number.isFinite(budget) ? "—" : `${Math.round(budget)}s`;
  if (
    used != null &&
    budget != null &&
    Number.isFinite(used) &&
    Number.isFinite(budget) &&
    used > budget
  ) {
    return `${usedLabel} cumulative / ${budgetLabel} chunk`;
  }
  return `${usedLabel} / ${budgetLabel}`;
}

function asCount(value: unknown): number {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return 0;
  return Math.trunc(number);
}
