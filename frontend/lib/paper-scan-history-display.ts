import { PaperScanRecord } from "./api";
import { marketLabel } from "./arbitrage-ops";
import { number } from "./format";
import {
  OBSERVATION_AGE_TICK_MS,
  formatObservationAge,
  observationTimestampTitle,
  parseObservationTimestampMs,
  startSharedObservationAgeTimer,
} from "./observation-age";

export const AUDIT_AGE_TICK_MS = OBSERVATION_AGE_TICK_MS;
export const parseAuditScannedAtMs = parseObservationTimestampMs;
export const formatAuditScanAge = formatObservationAge;
export const startSharedAuditAgeTimer = startSharedObservationAgeTimer;

export function auditScanTimestampTitle(scannedAt: string | null | undefined): string {
  return observationTimestampTitle(scannedAt, "scanned_at unavailable");
}

export const PAPER_SCAN_HISTORY_SORT_COLUMNS = [
  "age",
  "event",
  "market",
  "venues",
  "grossEdge",
  "netEdge",
  "executable",
  "guaranteedProfit",
  "risk",
  "mapping",
  "status",
] as const;

export type PaperScanHistorySortColumn = (typeof PAPER_SCAN_HISTORY_SORT_COLUMNS)[number];
export type SortDirection = "asc" | "desc";

export type PaperScanHistorySortState = {
  column: PaperScanHistorySortColumn;
  direction: SortDirection;
};

export const DEFAULT_PAPER_SCAN_HISTORY_SORT: PaperScanHistorySortState = {
  column: "age",
  direction: "desc",
};

export const PAPER_SCAN_HISTORY_SORT_LABELS: Record<PaperScanHistorySortColumn, string> = {
  age: "Scanned / Age",
  event: "Event",
  market: "Market",
  venues: "Venues",
  grossEdge: "Gross edge",
  netEdge: "Net edge",
  executable: "Executable",
  guaranteedProfit: "Guaranteed profit",
  risk: "Risk",
  mapping: "Mapping",
  status: "Status",
};

const NUMERIC_COLUMNS = new Set<PaperScanHistorySortColumn>([
  "grossEdge",
  "netEdge",
  "executable",
  "guaranteedProfit",
  "risk",
  "mapping",
]);

const TIME_COLUMNS = new Set<PaperScanHistorySortColumn>(["age"]);

export function paperScanHistoryStatusText(item: PaperScanRecord): string {
  if (item.eligible_for_paper_simulation) return "Paper eligible";
  if (item.rejection_reasons.length) return item.rejection_reasons.join(", ").replaceAll("_", " ");
  return item.is_arbitrage ? "Filtered" : "No arbitrage";
}

export function paperScanEventLabel(item: PaperScanRecord): string {
  return `${item.home_team} v ${item.away_team}`;
}

export function initialPaperScanHistoryDirection(column: PaperScanHistorySortColumn): SortDirection {
  if (TIME_COLUMNS.has(column) || NUMERIC_COLUMNS.has(column)) return "desc";
  return "asc";
}

export function nextPaperScanHistorySort(
  current: PaperScanHistorySortState | null,
  clicked: PaperScanHistorySortColumn,
): PaperScanHistorySortState {
  if (current?.column === clicked) {
    return { column: clicked, direction: current.direction === "asc" ? "desc" : "asc" };
  }
  return { column: clicked, direction: initialPaperScanHistoryDirection(clicked) };
}

export function ariaSortForPaperScanColumn(
  column: PaperScanHistorySortColumn,
  current: PaperScanHistorySortState | null,
): "ascending" | "descending" | "none" {
  if (!current || current.column !== column) return "none";
  return current.direction === "asc" ? "ascending" : "descending";
}

export function paperScanSortIndicator(
  column: PaperScanHistorySortColumn,
  current: PaperScanHistorySortState | null,
): "▲" | "▼" | "" {
  if (!current || current.column !== column) return "";
  return current.direction === "asc" ? "▲" : "▼";
}

function isMissing(value: number | string | null | undefined): boolean {
  if (value === null || value === undefined || value === "") return true;
  return typeof value === "number" && !Number.isFinite(value);
}

function sortValue(
  item: PaperScanRecord,
  column: PaperScanHistorySortColumn,
): number | string | null {
  switch (column) {
    case "age":
      return parseAuditScannedAtMs(item.scanned_at);
    case "event":
      return paperScanEventLabel(item).toLocaleLowerCase();
    case "market":
      return marketLabel(item).toLocaleLowerCase();
    case "venues":
      return item.venues.join(" / ").toLocaleLowerCase();
    case "grossEdge":
      return number(item.gross_edge);
    case "netEdge":
      return number(item.net_edge);
    case "executable":
      return number(item.executable_stake_gbp);
    case "guaranteedProfit":
      return number(item.guaranteed_profit_gbp);
    case "risk":
      return number(item.execution_risk_score);
    case "mapping": {
      const confidence = number(item.mapping_confidence);
      return confidence;
    }
    case "status":
      return paperScanHistoryStatusText(item).toLocaleLowerCase();
  }
}

export function sortPaperScanHistory(
  items: readonly PaperScanRecord[],
  sort: PaperScanHistorySortState | null,
): PaperScanRecord[] {
  const decorated = items.map((item, index) => ({ item, index }));
  decorated.sort((left, right) => {
    if (!sort) return left.index - right.index;
    const leftValue = sortValue(left.item, sort.column);
    const rightValue = sortValue(right.item, sort.column);
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
