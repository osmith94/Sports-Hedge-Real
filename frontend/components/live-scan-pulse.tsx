"use client";

export type LiveScanPulsePhase = "idle" | "scanning" | "complete" | "error" | "degraded" | "paused";

const VENUES: Array<{ key: string; label: string }> = [
  { key: "matchbook", label: "Matchbook" },
  { key: "polymarket", label: "Polymarket" },
  { key: "kalshi", label: "Kalshi" },
];

function venueTone(value: string | undefined): "ok" | "down" | "unknown" {
  if (!value) return "unknown";
  if (value === "ok") return "ok";
  if (value === "unavailable" || value === "error" || value === "failed") return "down";
  return "unknown";
}

function statusCopy(
  phase: LiveScanPulsePhase,
  nextRefreshSeconds: number | null,
): { title: string; detail: string } {
  if (phase === "scanning") {
    return { title: "Scanning live venues", detail: "Request in flight" };
  }
  if (phase === "complete") {
    return { title: "Live scan", detail: "Last refreshed just now" };
  }
  if (phase === "error") {
    return { title: "Scan failed", detail: "Not a healthy refresh" };
  }
  if (phase === "degraded") {
    return {
      title: "Live scan",
      detail:
        nextRefreshSeconds != null
          ? `Partial venue failure · next in ${nextRefreshSeconds}s`
          : "Partial venue failure",
    };
  }
  if (phase === "paused") {
    return { title: "Auto refresh off", detail: "Run Scan" };
  }
  return {
    title: "Live scan",
    detail:
      nextRefreshSeconds != null ? `Next refresh in ${nextRefreshSeconds}s` : "Waiting for first scan",
  };
}

export function LiveScanPulse({
  phase,
  nextRefreshSeconds,
  venueHealth,
  errorMessage,
}: {
  phase: LiveScanPulsePhase;
  nextRefreshSeconds: number | null;
  venueHealth?: Record<string, string> | null;
  errorMessage?: string | null;
}) {
  const copy = statusCopy(phase, nextRefreshSeconds);
  const showVenues = phase === "error" || phase === "degraded";
  return (
    <div
      className={`live-scan-pulse live-scan-pulse-${phase}`}
      role="status"
      aria-live="polite"
      aria-label={`${copy.title}. ${errorMessage ?? copy.detail}`}
    >
      <span className="live-scan-pulse-mark" aria-hidden="true">
        <span className="live-scan-pulse-ring" />
        <span className="live-scan-pulse-core" />
      </span>
      <span className="live-scan-pulse-copy">
        <span className="live-scan-pulse-title">{copy.title}</span>
        <span className="live-scan-pulse-detail">{errorMessage ?? copy.detail}</span>
        {showVenues ? (
          <span className="live-scan-pulse-venues">
            {VENUES.map((venue) => {
              const tone = venueTone(venueHealth?.[venue.key]);
              return (
                <span
                  key={venue.key}
                  className={`live-scan-pulse-venue live-scan-pulse-venue-${tone}`}
                  title={`${venue.label}: ${venueHealth?.[venue.key] ?? "unknown"}`}
                >
                  {venue.label}
                </span>
              );
            })}
          </span>
        ) : null}
      </span>
    </div>
  );
}
