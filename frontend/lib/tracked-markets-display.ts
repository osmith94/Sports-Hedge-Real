import { ArbitrageOpportunity } from "./arbitrage-ops";
import { distanceToSelectedThresholdPp } from "./comfort-threshold";
import { fixtureDetailHref } from "./discovered-fixture-display";

export const TRACKED_MARKET_SORT_COLUMNS = [
  "fixture",
  "status",
  "grossEdge",
  "netMargin",
  "backendTrigger",
  "distanceToBackendTrigger",
  "distanceToSelectedThreshold",
  "sourceUpdated",
] as const;

export type TrackedMarketSortColumn = (typeof TRACKED_MARKET_SORT_COLUMNS)[number];
export type SortDirection = "asc" | "desc";

export type TrackedMarketSortState = {
  column: TrackedMarketSortColumn;
  direction: SortDirection;
};

export const TRACKED_MARKET_SORT_LABELS: Record<TrackedMarketSortColumn, string> = {
  fixture: "Fixture / market",
  status: "Status",
  grossEdge: "Gross edge",
  netMargin: "Current net margin",
  backendTrigger: "Backend trigger",
  distanceToBackendTrigger: "Distance to backend trigger",
  distanceToSelectedThreshold: "Distance to selected threshold",
  sourceUpdated: "Source / last updated",
};

export const NUMERIC_TRACKED_MARKET_COLUMNS = new Set<TrackedMarketSortColumn>([
  "grossEdge",
  "netMargin",
  "backendTrigger",
  "distanceToBackendTrigger",
  "distanceToSelectedThreshold",
  "sourceUpdated",
]);

const ACTIONABLE_STATUSES = new Set(["TRIGGERED", "PAPER_FILLING", "PARTIAL"]);
const NEAR_STATUSES = new Set(["WATCHING", "APPROACHING"]);

export function trackedMarketHref(canonicalEventId: string | null | undefined): string | null {
  if (!canonicalEventId) return null;
  return fixtureDetailHref(canonicalEventId);
}

export function nextTrackedMarketSort(
  current: TrackedMarketSortState | null,
  clicked: TrackedMarketSortColumn,
): TrackedMarketSortState {
  if (current?.column === clicked) {
    return { column: clicked, direction: current.direction === "asc" ? "desc" : "asc" };
  }
  return { column: clicked, direction: "asc" };
}

export function ariaSortForColumn(
  column: TrackedMarketSortColumn,
  current: TrackedMarketSortState | null,
): "ascending" | "descending" | "none" {
  if (!current || current.column !== column) return "none";
  return current.direction === "asc" ? "ascending" : "descending";
}

export function sortIndicator(
  column: TrackedMarketSortColumn,
  current: TrackedMarketSortState | null,
): "▲" | "▼" | "" {
  if (!current || current.column !== column) return "";
  return current.direction === "asc" ? "▲" : "▼";
}

function isMissing(value: number | string | null | undefined): boolean {
  if (value === null || value === undefined || value === "") return true;
  return typeof value === "number" && !Number.isFinite(value);
}

function sortValue(
  item: ArbitrageOpportunity,
  column: TrackedMarketSortColumn,
  selectedThreshold: number,
): number | string | null {
  switch (column) {
    case "fixture":
      return `${item.eventLabel} ${item.marketLabel}`.trim().toLocaleLowerCase();
    case "status":
      return item.status;
    case "grossEdge":
      return item.grossArb;
    case "netMargin":
      return item.netArb;
    case "backendTrigger":
      return Number.isFinite(item.trigger) ? item.trigger : null;
    case "distanceToBackendTrigger":
      return item.distanceToTriggerPp;
    case "distanceToSelectedThreshold":
      return distanceToSelectedThresholdPp(item.netArb, selectedThreshold);
    case "sourceUpdated": {
      if (!item.scannedAt) return null;
      const timestamp = Date.parse(item.scannedAt);
      return Number.isFinite(timestamp) ? timestamp : null;
    }
  }
}

export function sortTrackedMarkets(
  items: readonly ArbitrageOpportunity[],
  sort: TrackedMarketSortState | null,
  selectedThreshold: number,
): ArbitrageOpportunity[] {
  const decorated = items.map((item, index) => ({ item, index }));
  decorated.sort((left, right) => {
    if (!sort) {
      const cmp = compareDefaultTrackedOrder(left.item, right.item);
      return cmp !== 0 ? cmp : left.index - right.index;
    }
    const leftValue = sortValue(left.item, sort.column, selectedThreshold);
    const rightValue = sortValue(right.item, sort.column, selectedThreshold);
    if (isMissing(leftValue) && isMissing(rightValue)) return left.index - right.index;
    if (isMissing(leftValue)) return 1;
    if (isMissing(rightValue)) return -1;
    if (typeof leftValue === "number" && typeof rightValue === "number") {
      const delta = leftValue - rightValue;
      const cmp = delta === 0 ? 0 : sort.direction === "asc" ? (delta < 0 ? -1 : 1) : delta < 0 ? 1 : -1;
      return cmp !== 0 ? cmp : left.index - right.index;
    }
    const cmp = String(leftValue).localeCompare(String(rightValue), undefined, {
      numeric: true,
      sensitivity: "base",
    });
    const directed = sort.direction === "asc" ? cmp : -cmp;
    return directed !== 0 ? directed : left.index - right.index;
  });
  return decorated.map((entry) => entry.item);
}

export function compareDefaultTrackedOrder(
  left: ArbitrageOpportunity,
  right: ArbitrageOpportunity,
): number {
  const leftKey = defaultTrackedRankKey(left);
  const rightKey = defaultTrackedRankKey(right);
  for (let index = 0; index < leftKey.length; index += 1) {
    const leftPart = leftKey[index];
    const rightPart = rightKey[index];
    if (leftPart === rightPart) continue;
    if (typeof leftPart === "number" && typeof rightPart === "number") {
      return leftPart < rightPart ? -1 : 1;
    }
    return String(leftPart).localeCompare(String(rightPart));
  }
  return 0;
}

function defaultTrackedRankKey(
  item: ArbitrageOpportunity,
): [number, number, number, string, string] {
  if (ACTIONABLE_STATUSES.has(item.status)) {
    const missing = item.netArb === null ? 1 : 0;
    const edge = item.netArb === null ? 0 : -item.netArb;
    return [0, missing, edge, item.marketLabel, item.id];
  }
  if (NEAR_STATUSES.has(item.status)) {
    const missing = item.distanceToTriggerPp === null ? 1 : 0;
    const distance = item.distanceToTriggerPp === null ? 0 : item.distanceToTriggerPp;
    return [1, missing, distance, item.marketLabel, item.id];
  }
  return [2, 0, 0, item.marketLabel, item.id];
}

export function shouldNavigateFromRowClick(event: {
  button?: number;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  defaultPrevented?: boolean;
  selectedText?: string;
  target?: { closest?: (selector: string) => unknown } | null;
}): boolean {
  if (event.defaultPrevented) return false;
  if ((event.button ?? 0) !== 0) return false;
  if (event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return false;
  if (event.selectedText) return false;
  if (event.target?.closest?.("a, button, input, select, textarea, label, [role='button']")) {
    return false;
  }
  return true;
}
