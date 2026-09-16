export const VENUE_HEALTH_DISABLED = "disabled";

export const PROVIDER_HEALTH_FAILURES = new Set([
  "unavailable",
  "timeout",
  "degraded",
  "error",
  "failed",
]);

export type VenueHealthTone = "ok" | "warn" | "down" | "unknown" | "off";

export function isOperatorDisabledHealth(value: string | undefined): boolean {
  return value === VENUE_HEALTH_DISABLED;
}

export function isProviderHealthFailure(value: string | undefined): boolean {
  return Boolean(value && PROVIDER_HEALTH_FAILURES.has(value));
}

export function venueHealthIsDegraded(health: Record<string, string> | undefined): boolean {
  if (!health) return false;
  return ["matchbook", "polymarket", "kalshi"].some((venue) =>
    isProviderHealthFailure(health[venue]),
  );
}

export function scanHealthTone(value: string | undefined): VenueHealthTone | null {
  if (!value) return null;
  if (value === "ok") return "ok";
  if (value === "degraded" || value === "timeout") return "warn";
  if (value === "unavailable" || value === "error" || value === "failed") return "down";
  if (value === VENUE_HEALTH_DISABLED) return "off";
  return "unknown";
}

export function pulseVenueTone(value: string | undefined): "ok" | "down" | "unknown" | "off" {
  if (!value) return "unknown";
  if (value === "ok") return "ok";
  if (isOperatorDisabledHealth(value)) return "off";
  if (isProviderHealthFailure(value)) return "down";
  return "unknown";
}

export function venueHealthCaption(
  label: string,
  scan?: string,
  row?: { ok?: boolean; authenticated?: boolean } | null,
): string {
  if (isOperatorDisabledHealth(scan)) return `${label} off (operator)`;
  if (scan === "degraded") return `${label} degraded`;
  if (scan === "timeout") return `${label} timeout`;
  if (scan === "unavailable" || scan === "error" || scan === "failed") {
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
