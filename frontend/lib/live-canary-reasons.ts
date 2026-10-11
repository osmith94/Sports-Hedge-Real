const FALLBACK: Record<string, string> = {
  geoblock_blocked: "Polymarket reports this current IP is blocked (territory rejection).",
  geoblock_unreachable:
    "Polymarket territory check could not be reached (not a location rejection).",
  geoblock_unreachable_timeout:
    "Polymarket territory check timed out (not a location rejection).",
  geoblock_unreachable_network:
    "Polymarket territory check could not be reached (not a location rejection).",
  geoblock_unreachable_http:
    "Polymarket territory check returned a non-200 HTTP status (not a location rejection).",
  matchbook_currency_mismatch:
    "Matchbook account currency conflicts with the configured native execution currency.",
  matchbook_currency_unknown:
    "Matchbook account currency not included in official balance; supported configured-native currency evidence governs.",
};

export function explainLiveCanaryReason(reason: string): string {
  if (FALLBACK[reason]) return FALLBACK[reason];
  if (reason.startsWith("geoblock_unreachable")) {
    return FALLBACK.geoblock_unreachable;
  }
  return reason;
}

export function explainLiveCanaryFailure(
  reasons: string[] | undefined,
  explanations?: string[] | undefined,
): string {
  if (explanations && explanations.length > 0) {
    return explanations.join(" ");
  }
  if (!reasons || reasons.length === 0) {
    return "NOT READY";
  }
  return reasons.map(explainLiveCanaryReason).join(" ");
}
