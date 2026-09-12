import type { AlertSeverity, CurrencyCode, FillConfidenceBand } from "./types";

export function money(value: number, currency: CurrencyCode): string {
  return new Intl.NumberFormat(currency === "GBP" ? "en-GB" : "en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function percent(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

export function quoteAge(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

export function venueLabel(venue: string): string {
  return venue.charAt(0).toUpperCase() + venue.slice(1);
}

export function severityTone(severity: AlertSeverity): string {
  if (severity === "CRITICAL") return "critical";
  if (severity === "HIGH_PRIORITY") return "high";
  return "priority";
}

export function confidenceTone(band: FillConfidenceBand): string {
  if (band === "HIGH") return "high";
  if (band === "MEDIUM") return "medium";
  return "low";
}
