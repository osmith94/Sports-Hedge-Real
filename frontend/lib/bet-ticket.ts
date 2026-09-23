import { ArbitrageOpportunity } from "./arbitrage-ops";
import { PreparablePaperOpportunity } from "./api";

export function fixtureBetHref(canonicalEventId: string, opportunityId: string): string {
  const event = encodeURIComponent(canonicalEventId);
  const bet = encodeURIComponent(opportunityId);
  return `/arbitrage/fixtures/${event}?bet=${bet}#bet-ticket`;
}

export function opportunityBetHref(item: ArbitrageOpportunity): string | null {
  if (!item.canonicalEventId) return null;
  return fixtureBetHref(item.canonicalEventId, item.id);
}

export function isBetActionable(
  item: Pick<ArbitrageOpportunity, "betActionable" | "executable" | "status">,
): boolean {
  if (item.betActionable === true) return true;
  if (item.betActionable === false) return false;
  return Boolean(item.executable && item.status === "TRIGGERED");
}

export function betBlockedReason(item: ArbitrageOpportunity): string {
  if (item.betBlockedReason) return item.betBlockedReason.replaceAll("_", " ");
  if (item.status === "REJECTED") {
    return item.riskFlags[0]?.replaceAll("_", " ") || "rejected";
  }
  if (item.status === "WATCHING" || item.status === "APPROACHING") return "below trigger";
  if (!item.canonicalEventId) return "missing fixture identity";
  return "not preparable";
}

export function recommendedSizeText(
  item: PreparablePaperOpportunity,
): string | null {
  const value = item.recommended_size_gbp;
  if (value === null || value === undefined || value === "") return null;
  return String(value);
}
