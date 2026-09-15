import { LiveRefreshStatus, NearOpportunity, Venue, WatchLeg } from "./api";
import { money, number, percent } from "./format";
import {
  MappingProvenance,
  MappingReviewCandidate,
  hasSafeReviewCandidate,
  mappingConfidencePercent,
  mappingProvenanceLabel,
  shouldOfferMappingVerifyAction,
} from "./mapping-verification";
import {
  formatObservationAge,
  parseObservationTimestampMs,
} from "./observation-age";
import { fastScanCopy, fullSweepCopy } from "./scan-status-display";
import { trackedMarketHref } from "./tracked-markets-display";

export const OPPORTUNITY_MONITOR_SORT_COLUMNS = [
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
  "state",
  "lane",
] as const;

export type OpportunityMonitorSortColumn = (typeof OPPORTUNITY_MONITOR_SORT_COLUMNS)[number];
export type SortDirection = "asc" | "desc";

export type OpportunityMonitorSortState = {
  column: OpportunityMonitorSortColumn;
  direction: SortDirection;
};

export const OPPORTUNITY_MONITOR_SORT_LABELS: Record<OpportunityMonitorSortColumn, string> = {
  age: "Age",
  event: "Event",
  market: "Market",
  venues: "Venue legs",
  grossEdge: "Gross edge",
  netEdge: "Net edge",
  executable: "Executable size",
  guaranteedProfit: "Guaranteed profit",
  risk: "Risk",
  mapping: "Mapping",
  state: "State",
  lane: "Lane / freshness",
};

export type OpportunityMonitorStateBadge =
  | "QUALIFYING"
  | "NEAR"
  | "BELOW BREAK-EVEN"
  | "REJECTED"
  | "STALE";

export type OpportunityMonitorLegView = {
  outcome: string;
  venue: string;
  sourceMarketId: string;
  sourceRunnerId: string;
  action: string;
  price: string;
  executableDepth: string;
  stake: string;
  freshness: string;
};

export type OpportunityMonitorRow = {
  id: string;
  canonicalEventId: string | null;
  href: string | null;
  observedAt: string | null;
  eventLabel: string;
  marketLabel: string;
  venuesLabel: string;
  venues: Venue[];
  grossEdge: number | null;
  netEdge: number | null;
  executableSizeGbp: number | null;
  guaranteedProfitGbp: number | null;
  riskScore: number | null;
  mappingText: string;
  mappingTitle: string;
  mappingConfidence: number | null;
  offerVerify: boolean;
  mappingCandidate: MappingReviewCandidate | null;
  mappingProvenance: MappingProvenance | null;
  state: OpportunityMonitorStateBadge;
  stateTitle: string;
  laneLabel: string;
  freshnessLabel: string;
  quoteAgeLabel: string;
  rejectionDetail: string;
  legs: OpportunityMonitorLegView[];
};

export type OpportunityMonitorSummary = {
  qualifyingCount: number | null;
  nearCount: number | null;
  bestNetEdge: number | null;
  newestObservedAt: string | null;
  activeVenues: string;
  fastScan: string;
  fullSweep: string;
  dataClass: string;
};

const NUMERIC_COLUMNS = new Set<OpportunityMonitorSortColumn>([
  "grossEdge",
  "netEdge",
  "executable",
  "guaranteedProfit",
  "risk",
]);

const TIME_COLUMNS = new Set<OpportunityMonitorSortColumn>(["age"]);

const QUALIFYING_STATUSES = new Set(["TRIGGERED"]);
const NEAR_STATUSES = new Set(["WATCHING", "APPROACHING"]);
const REJECTED_STATUSES = new Set(["REJECTED", "EXPIRED"]);
const CAPTURED_LIFECYCLE_STATUSES = new Set(["PAPER_FILLING", "PARTIAL"]);
const STALE_REASONS = new Set(["stale_quote", "rejected_stale_quote", "unknown_quote_age"]);

export const MAPPING_UNAVAILABLE_TITLE =
  "Current mapping confidence/provenance is not on this radar observation";

export function opportunityObservationTimestamp(item: NearOpportunity): string | null {
  return item.last_scanned_at || item.last_seen_at || null;
}

export function opportunityMarketLabel(item: NearOpportunity): string {
  if (!item.market_family) return "—";
  const family = item.market_family.replaceAll("_", " ");
  const period = item.period ? ` · ${item.period.replaceAll("_", " ")}` : "";
  return `${family}${period}`;
}

export function opportunityEventLabel(item: NearOpportunity): string {
  const home = item.home_team?.trim() || "Unknown";
  const away = item.away_team?.trim() || "Unknown";
  return `${home} v ${away}`;
}

export function venueLegsLabel(item: NearOpportunity): string {
  if (item.venues.length) return item.venues.join(" / ");
  const fromLegs = [...new Set(item.legs.map((leg) => leg.venue))];
  return fromLegs.length ? fromLegs.join(" / ") : "—";
}

export function scanLaneLabel(scanLane: string | null | undefined): string {
  if (!scanLane) return "—";
  const lane = scanLane.trim().toLowerCase();
  if (lane === "hot") return "Fast Scan / HOT";
  if (lane === "universe") return "Full Sweep / UNIVERSE";
  return scanLane;
}

export function freshnessLabel(freshness: string | null | undefined): string {
  if (!freshness) return "—";
  return freshness.replaceAll("_", " ");
}

export function quoteAgeLabel(item: NearOpportunity): string {
  if (item.quote_age_ms == null) {
    return item.quote_age_basis ? `unknown · ${item.quote_age_basis}` : "—";
  }
  const age =
    item.quote_age_ms < 1000 ? `${item.quote_age_ms}ms` : `${(item.quote_age_ms / 1000).toFixed(1)}s`;
  const basis = item.quote_age_basis ? ` · ${item.quote_age_basis}` : "";
  return `${age}${basis} at last evaluation`;
}

export function isExecutableRadarFreshness(freshness: string | null | undefined): boolean {
  return (freshness || "").toLowerCase() === "executable";
}

export function isPreTradeTrigger(item: NearOpportunity): boolean {
  return QUALIFYING_STATUSES.has(item.status);
}

export function isCapturedLifecycleStatus(status: string | null | undefined): boolean {
  return CAPTURED_LIFECYCLE_STATUSES.has(status ?? "");
}

export function visibleOpportunityMonitorItems(
  items: readonly NearOpportunity[],
): NearOpportunity[] {
  return items.filter((item) => !isCapturedLifecycleStatus(item.status));
}

export function opportunityMonitorRows(
  items: readonly NearOpportunity[],
): OpportunityMonitorRow[] {
  return visibleOpportunityMonitorItems(items).map(opportunityMonitorRow);
}

export function opportunityMonitorState(item: NearOpportunity): OpportunityMonitorStateBadge {
  const freshness = (item.freshness_class || "").toLowerCase();
  const reasons = [...item.rejection_reasons, ...item.insufficiency_reasons].map((reason) =>
    reason.toLowerCase(),
  );
  if (freshness === "expired" || item.status === "EXPIRED" || reasons.some((reason) => STALE_REASONS.has(reason))) {
    return "STALE";
  }
  if (isPreTradeTrigger(item) && isExecutableRadarFreshness(item.freshness_class)) {
    return "QUALIFYING";
  }
  // Historical TRIGGERED remaining on radar after the ~1s executable gate.
  // Do not infer executable freshness from stored status, is_arbitrage, or quote_age_ms.
  if (isPreTradeTrigger(item)) {
    return "STALE";
  }
  const net = number(item.current_net_edge);
  if (net !== null && net < 0) {
    return "BELOW BREAK-EVEN";
  }
  if (REJECTED_STATUSES.has(item.status) || item.classification === "rejected") {
    return "REJECTED";
  }
  if (NEAR_STATUSES.has(item.status)) {
    return "NEAR";
  }
  if (reasons.length) return "REJECTED";
  return "NEAR";
}

export function opportunityMonitorStateTone(
  state: OpportunityMonitorStateBadge,
): "hot" | "watch" | "reject" | "stale" {
  if (state === "QUALIFYING") return "hot";
  if (state === "NEAR") return "watch";
  if (state === "STALE") return "stale";
  return "reject";
}

export function opportunityMonitorStateTitle(item: NearOpportunity): string {
  const reasons = [...item.rejection_reasons, ...item.insufficiency_reasons]
    .map((reason) => reason.replaceAll("_", " "))
    .join("; ");
  const status = item.status.replaceAll("_", " ");
  const classification = item.classification.replaceAll("_", " ");
  const parts = [status, classification];
  if (reasons) parts.push(reasons);
  if (isPreTradeTrigger(item) && !isExecutableRadarFreshness(item.freshness_class)) {
    const freshness = freshnessLabel(item.freshness_class);
    parts.push(
      freshness === "—"
        ? "not currently executable · freshness unknown"
        : `not currently executable · ${freshness}`,
    );
  }
  return parts.join(" · ");
}

export function mappingDisplay(item?: NearOpportunity | null): {
  text: string;
  title: string;
  offerVerify: boolean;
  candidate: MappingReviewCandidate | null;
  provenance: MappingProvenance | null;
  confidence: number | null;
} {
  const confidence = item?.mapping_confidence;
  const candidate = hasSafeReviewCandidate(item?.mapping_review_candidate)
    ? item?.mapping_review_candidate ?? null
    : null;
  const provenance = (item?.mapping_provenance as MappingProvenance | null | undefined) ?? null;
  if (confidence == null || !Number.isFinite(confidence)) {
    return {
      text: "—",
      title: MAPPING_UNAVAILABLE_TITLE,
      offerVerify: false,
      candidate: null,
      provenance: null,
      confidence: null,
    };
  }
  const provenanceLabel = mappingProvenanceLabel(provenance);
  const reasons = (item?.mapping_reasons ?? []).join("; ");
  const titleParts = [
    mappingConfidencePercent(confidence),
    provenanceLabel,
    reasons,
  ].filter(Boolean);
  return {
    text: `${mappingConfidencePercent(confidence)} · ${provenanceLabel}`,
    title: titleParts.join(" · "),
    offerVerify: shouldOfferMappingVerifyAction(confidence, candidate),
    candidate,
    provenance,
    confidence,
  };
}

export function opportunityLegViews(item: NearOpportunity): OpportunityMonitorLegView[] {
  const freshness = quoteAgeLabel(item);
  return item.legs.map((leg) => opportunityLegView(leg, freshness));
}

export function opportunityLegView(leg: WatchLeg, freshness: string): OpportunityMonitorLegView {
  const gbpStake = number(leg.gbp_stake);
  const nativeStake = number(leg.native_stake);
  let stake = "—";
  if (gbpStake !== null) {
    stake = money(gbpStake);
  } else if (nativeStake !== null) {
    stake = `${money(nativeStake, leg.currency === "USD" ? "USD" : "GBP")} ${leg.currency}`;
  }
  return {
    outcome: leg.outcome || "—",
    venue: leg.venue || "—",
    sourceMarketId: leg.source_market_id || "—",
    sourceRunnerId: leg.source_runner_id || "—",
    // WatchLeg has no BACK/LAY/BUY/SELL action. Do not invent one.
    action: "—",
    price: leg.net_decimal_odds == null ? "—" : String(leg.net_decimal_odds),
    executableDepth:
      leg.cumulative_depth_gbp == null ? "—" : money(leg.cumulative_depth_gbp),
    stake,
    freshness,
  };
}

export function formatOpportunityLegLine(leg: OpportunityMonitorLegView): string {
  return `${leg.outcome} — ${leg.venue} @ ${leg.price} · action ${leg.action} · depth ${leg.executableDepth} · stake ${leg.stake} · source ${leg.sourceMarketId} · freshness ${leg.freshness}`;
}

export function opportunityMonitorRow(item: NearOpportunity): OpportunityMonitorRow {
  const mapping = mappingDisplay(item);
  return {
    id: item.opportunity_id,
    canonicalEventId: item.canonical_event_id || null,
    href: trackedMarketHref(item.canonical_event_id),
    observedAt: opportunityObservationTimestamp(item),
    eventLabel: opportunityEventLabel(item),
    marketLabel: opportunityMarketLabel(item),
    venuesLabel: venueLegsLabel(item),
    venues: item.venues,
    grossEdge: number(item.gross_edge),
    netEdge: number(item.current_net_edge),
    executableSizeGbp: number(item.limiting_depth_gbp),
    guaranteedProfitGbp: number(item.guaranteed_profit_gbp),
    riskScore: item.execution_risk_score ?? null,
    mappingText: mapping.text,
    mappingTitle: mapping.title,
    mappingConfidence: mapping.confidence,
    offerVerify: mapping.offerVerify,
    mappingCandidate: mapping.candidate,
    mappingProvenance: mapping.provenance,
    state: opportunityMonitorState(item),
    stateTitle: opportunityMonitorStateTitle(item),
    laneLabel: scanLaneLabel(item.scan_lane),
    freshnessLabel: freshnessLabel(item.freshness_class),
    quoteAgeLabel: quoteAgeLabel(item),
    rejectionDetail: opportunityMonitorStateTitle(item),
    legs: opportunityLegViews(item),
  };
}

export function activeVenueSetLabel(
  status: LiveRefreshStatus | null,
  available: boolean,
): string {
  if (!available || !status) return "—";
  const listed = [
    ...(status.hot?.active_venues ?? []),
    ...(status.universe?.active_venues ?? []),
  ];
  const unique = [...new Set(listed)];
  if (status.hot?.active_venues == null && status.universe?.active_venues == null) {
    return "—";
  }
  if (unique.length === 0) return "no venues";
  return unique.join(" · ");
}

export function opportunityMonitorSummary(
  rows: readonly OpportunityMonitorRow[],
  status: LiveRefreshStatus | null,
  available: boolean,
  liveRefreshAvailable: boolean,
  nowMs = Date.now(),
): OpportunityMonitorSummary {
  const fast = liveRefreshAvailable ? fastScanCopy(status, nowMs) : null;
  const full = liveRefreshAvailable ? fullSweepCopy(status, nowMs) : null;
  const fastScan = fast ? `${fast.label} · ${fast.detail}` : "—";
  const fullSweep = full ? `${full.label} · ${full.detail}` : "—";
  if (!available) {
    return {
      qualifyingCount: null,
      nearCount: null,
      bestNetEdge: null,
      newestObservedAt: null,
      activeVenues: activeVenueSetLabel(status, liveRefreshAvailable),
      fastScan,
      fullSweep,
      dataClass: "UNAVAILABLE",
    };
  }
  const qualifyingCount = rows.filter((row) => row.state === "QUALIFYING").length;
  const nearCount = rows.filter((row) => row.state === "NEAR").length;
  const edges = rows.map((row) => row.netEdge).filter((value): value is number => value !== null);
  const newest = rows.reduce<string | null>((best, row) => {
    const currentMs = parseObservationTimestampMs(row.observedAt);
    const bestMs = parseObservationTimestampMs(best);
    if (currentMs === null) return best;
    if (bestMs === null || currentMs > bestMs) return row.observedAt;
    return best;
  }, null);
  return {
    qualifyingCount,
    nearCount,
    bestNetEdge: edges.length ? Math.max(...edges) : null,
    newestObservedAt: newest,
    activeVenues: activeVenueSetLabel(status, liveRefreshAvailable),
    fastScan,
    fullSweep,
    dataClass: "LIVE PAPER",
  };
}

export function newestObservationAgeLabel(
  summary: OpportunityMonitorSummary,
  nowMs: number,
): string {
  if (summary.newestObservedAt == null) return "—";
  return formatObservationAge(summary.newestObservedAt, nowMs);
}

export function initialOpportunityMonitorDirection(
  column: OpportunityMonitorSortColumn,
): SortDirection {
  if (TIME_COLUMNS.has(column) || NUMERIC_COLUMNS.has(column)) return "desc";
  return "asc";
}

export function nextOpportunityMonitorSort(
  current: OpportunityMonitorSortState | null,
  clicked: OpportunityMonitorSortColumn,
): OpportunityMonitorSortState {
  if (current?.column === clicked) {
    return { column: clicked, direction: current.direction === "asc" ? "desc" : "asc" };
  }
  return { column: clicked, direction: initialOpportunityMonitorDirection(clicked) };
}

export function ariaSortForOpportunityColumn(
  column: OpportunityMonitorSortColumn,
  current: OpportunityMonitorSortState | null,
): "ascending" | "descending" | "none" {
  if (!current || current.column !== column) return "none";
  return current.direction === "asc" ? "ascending" : "descending";
}

export function opportunitySortIndicator(
  column: OpportunityMonitorSortColumn,
  current: OpportunityMonitorSortState | null,
): "▲" | "▼" | "" {
  if (!current || current.column !== column) return "";
  return current.direction === "asc" ? "▲" : "▼";
}

function isMissing(value: number | string | null | undefined): boolean {
  if (value === null || value === undefined || value === "") return true;
  return typeof value === "number" && !Number.isFinite(value);
}

function defaultStateRank(state: OpportunityMonitorStateBadge): number {
  if (state === "QUALIFYING") return 0;
  if (state === "NEAR") return 1;
  if (state === "STALE") return 3;
  return 2;
}

export function compareDefaultOpportunityOrder(
  left: OpportunityMonitorRow,
  right: OpportunityMonitorRow,
): number {
  const stateDelta = defaultStateRank(left.state) - defaultStateRank(right.state);
  if (stateDelta !== 0) return stateDelta;
  const leftMissing = left.netEdge === null ? 1 : 0;
  const rightMissing = right.netEdge === null ? 1 : 0;
  if (leftMissing !== rightMissing) return leftMissing - rightMissing;
  if (left.netEdge !== null && right.netEdge !== null && left.netEdge !== right.netEdge) {
    return right.netEdge - left.netEdge;
  }
  const leftAge = parseObservationTimestampMs(left.observedAt);
  const rightAge = parseObservationTimestampMs(right.observedAt);
  if (leftAge === null && rightAge === null) return left.id.localeCompare(right.id);
  if (leftAge === null) return 1;
  if (rightAge === null) return -1;
  if (leftAge !== rightAge) return rightAge - leftAge;
  return left.id.localeCompare(right.id);
}

function sortValue(
  row: OpportunityMonitorRow,
  column: OpportunityMonitorSortColumn,
): number | string | null {
  switch (column) {
    case "age":
      return parseObservationTimestampMs(row.observedAt);
    case "event":
      return row.eventLabel.toLocaleLowerCase();
    case "market":
      return row.marketLabel.toLocaleLowerCase();
    case "venues":
      return row.venuesLabel.toLocaleLowerCase();
    case "grossEdge":
      return row.grossEdge;
    case "netEdge":
      return row.netEdge;
    case "executable":
      return row.executableSizeGbp;
    case "guaranteedProfit":
      return row.guaranteedProfitGbp;
    case "risk":
      return row.riskScore;
    case "mapping":
      if (row.mappingConfidence == null || row.mappingText === "—") return null;
      return row.mappingText.toLocaleLowerCase();
    case "state":
      return row.state;
    case "lane":
      return `${row.laneLabel} ${row.freshnessLabel}`.toLocaleLowerCase();
  }
}

export function sortOpportunityMonitor(
  rows: readonly OpportunityMonitorRow[],
  sort: OpportunityMonitorSortState | null,
): OpportunityMonitorRow[] {
  const decorated = rows.map((item, index) => ({ item, index }));
  decorated.sort((left, right) => {
    if (!sort) {
      const cmp = compareDefaultOpportunityOrder(left.item, right.item);
      return cmp !== 0 ? cmp : left.index - right.index;
    }
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

export function formatMonitorPercent(value: number | null): string {
  return percent(value);
}

export function formatMonitorMoney(value: number | null): string {
  return money(value);
}
