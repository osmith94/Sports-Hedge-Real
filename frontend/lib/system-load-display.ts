import { SystemLoadSummary } from "./api";
import { money } from "./format";

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
  const universe = load.universe ?? {};
  const active = load.active_trade ?? {};
  const overdue = asCount(active.overdue);
  const overdueBit = overdue > 0 ? ` · ${overdue} overdue` : "";
  const locked = active.capital_locked_gbp == null || active.capital_locked_gbp === ""
    ? ""
    : ` · ${money(active.capital_locked_gbp)} locked`;
  const hotCount = hot.pricing_fixtures != null ? asCount(hot.pricing_fixtures) : asCount(hot.fixtures);
  const backgroundCount = background.pricing_fixtures != null
    ? `${asCount(background.pricing_fixtures)} fixtures`
    : `${asCount(background.working_set)} items`;
  const universeState = laneState(universe.worker_state, universe.health);
  const chunk = universe.generation_budget_seconds != null && Number.isFinite(Number(universe.generation_budget_seconds))
    ? `${Math.round(Number(universe.generation_budget_seconds))}s chunk`
    : null;
  return [
    {
      key: "HOT",
      detail: joinBits([
        `${hotCount} fixtures`,
        presentHealth(hot.health),
        formatCadence(hot.cadence_seconds),
      ]),
    },
    {
      key: "BACKGROUND",
      detail: joinBits([
        backgroundCount,
        presentHealth(background.health),
        formatCadence(background.cadence_seconds),
      ]),
    },
    {
      key: "UNIVERSE",
      detail: joinBits([
        `${asCount(universe.evaluated)}/${asCount(universe.total)}`,
        universeState,
        chunk,
      ]),
    },
    {
      key: "ACTIVE TRADES",
      detail: `${asCount(active.open_trades)} open${overdueBit}${locked}`,
    },
  ];
}

export function systemLoadDetailLines(
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
      detail: `${asCount(background.working_set)} items · ${asCount(background.due)} due · scan interval ${formatCadence(background.cadence_seconds)}`,
    },
    {
      key: "UNIVERSE discovery",
      detail: [
        uniProgress,
        `discovery refresh ${formatCadence(universe.cadence_seconds)}`,
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

function presentHealth(health: string | null | undefined): string | null {
  if (!health || health === "unknown") return null;
  return health;
}

function laneState(worker: string | null | undefined, health: string | null | undefined): string | null {
  if (worker && worker !== "unknown") return worker;
  return presentHealth(health);
}

function joinBits(bits: Array<string | null | undefined>): string {
  return bits.filter((bit) => Boolean(bit)).join(" · ");
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
