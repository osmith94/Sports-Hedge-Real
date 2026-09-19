export const VENUE_HEALTH_DISABLED = "disabled";

export const PROVIDER_HEALTH_FAILURES = new Set([
  "unavailable",
  "timeout",
  "discovery_timeout",
  "market_timeout",
  "auth_failure",
  "degraded",
  "error",
  "failed",
]);

const UI_DEGRADED_EXTRA = new Set(["retry_wait", "partial"]);

export type VenueHealthTone = "ok" | "warn" | "down" | "unknown" | "off";

export function isOperatorDisabledHealth(value: string | undefined): boolean {
  return value === VENUE_HEALTH_DISABLED;
}

export function isProviderHealthFailure(value: string | undefined): boolean {
  return Boolean(value && PROVIDER_HEALTH_FAILURES.has(value));
}

export function isUiDegradedHealth(value: string | undefined): boolean {
  if (!value || isOperatorDisabledHealth(value)) return false;
  return isProviderHealthFailure(value) || UI_DEGRADED_EXTRA.has(value);
}

export function venueHealthIsDegraded(health: Record<string, string> | undefined): boolean {
  if (!health) return false;
  return ["matchbook", "polymarket", "kalshi"].some((venue) => isUiDegradedHealth(health[venue]));
}

export function venueHealthNeedsWhy(value: string | undefined): boolean {
  return isUiDegradedHealth(value);
}

export function scanHealthTone(value: string | undefined): VenueHealthTone | null {
  if (!value) return null;
  if (value === "ok") return "ok";
  if (
    value === "degraded" ||
    value === "timeout" ||
    value === "discovery_timeout" ||
    value === "market_timeout" ||
    value === "retry_wait" ||
    value === "partial"
  ) {
    return "warn";
  }
  if (value === "unavailable" || value === "error" || value === "failed" || value === "auth_failure") {
    return "down";
  }
  if (value === VENUE_HEALTH_DISABLED) return "off";
  return "unknown";
}

export function pulseVenueTone(value: string | undefined): "ok" | "down" | "unknown" | "off" {
  if (!value) return "unknown";
  if (value === "ok") return "ok";
  if (isOperatorDisabledHealth(value)) return "off";
  if (isUiDegradedHealth(value)) return "down";
  return "unknown";
}

export function venueHealthCaption(
  label: string,
  scan?: string,
  row?: { ok?: boolean; authenticated?: boolean } | null,
): string {
  if (isOperatorDisabledHealth(scan)) return `${label} off (operator)`;
  if (scan === "degraded" || scan === "partial") return `${label} degraded`;
  if (scan === "timeout") return `${label} timeout`;
  if (scan === "discovery_timeout") return `${label} discovery timeout`;
  if (scan === "market_timeout") return `${label} market timeout`;
  if (scan === "retry_wait") return `${label} retry wait`;
  if (scan === "unavailable" || scan === "error" || scan === "failed" || scan === "auth_failure") {
    return `${label} unavailable`;
  }
  if (scan === "ok") return healthyFeedCaption(label);
  if (!row) return `${label} health unknown`;
  if (!row.ok) return `${label} unavailable`;
  return healthyFeedCaption(label);
}

export function healthyFeedCaption(label: string): string {
  if (label === "Kalshi") return "Kalshi live data";
  return `${label} data`;
}
