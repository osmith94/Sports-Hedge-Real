import type { StreamCandidateStatus, StreamStatus } from "./api";

export const STREAM_HEADING = "STREAM";
export const STREAM_KICKER = "STREAM";
export const STREAM_COPY =
  "One pinned fixture. Populated Polymarket books plus coalesced Matchbook exact-ID depth feed existing Price-2. Execution stays off unless separately armed.";
export const STREAM_EMPTY = "No fixture selected. STREAM stays off until Send to STREAM.";
export const STREAM_SHADOW_LABEL = "EXECUTION OFF";
export const STREAM_LIVE_LABEL = "EXECUTION ON";

export function streamExecutionLabel(enabled: boolean | undefined): string {
  return enabled === true ? STREAM_LIVE_LABEL : STREAM_SHADOW_LABEL;
}
export const SEND_TO_STREAM_LABEL = "Send to STREAM";
export const STREAM_HELP =
  "Data validity is book completeness and freshness. Economics is net-edge after fees and FX. Price-2 accepted is not a fill. Paper fills are simulated. Venue orders require real mode and SPORTS_HEDGE_EXECUTION_ENABLED.";

export type StreamRibbonItem = {
  key: string;
  label: string;
  value: string;
  tone?: "ok" | "warn" | "muted";
};

export function streamStatusLabel(status: StreamStatus | null | undefined): string {
  const raw = String(status?.connection_status || "not_selected").replaceAll("_", " ");
  return raw.toUpperCase();
}

export function streamStatusTone(status: StreamStatus | null | undefined): "ok" | "warn" | "off" | "error" {
  const connection = String(status?.connection_status || "not_selected");
  if (connection === "subscribed") return "ok";
  if (connection === "connecting" || connection === "degraded" || connection === "stale") return "warn";
  if (connection === "paused" || connection === "not_selected" || connection === "disabled") return "off";
  return "error";
}

export function streamFixtureName(status: StreamStatus | null | undefined): string {
  if (!status?.canonical_event_id) return STREAM_EMPTY;
  const home = status.home_team || "Home";
  const away = status.away_team || "Away";
  return `${home} v ${away}`;
}

export function streamCandidateSummary(status: StreamStatus | null | undefined): string {
  const current = (status?.candidates ?? []).filter((item) => item.current !== false);
  if (!status?.canonical_event_id) return STREAM_EMPTY;
  if (!status.candidates_current) {
    return "Last comparisons are cached, not current market data.";
  }
  if (!current.length) return "No STREAM candidate edges.";
  const qualified = current.filter((item) => item.economics_state === "qualified" || item.trustworthy);
  const price2 = current.filter((item) => item.price2);
  if (price2.length) {
    const fills = status.paper_opened ? "paper fill recorded" : "no persisted paper fill";
    const orders = status.orders_placed ? "venue orders recorded" : "no venue orders recorded";
    return `${price2.length} Price-2 accepted · ${fills} · ${orders}`;
  }
  if (qualified.length) {
    return `${qualified.length} net-edge qualified · waiting for Price-2`;
  }
  const valid = current.filter((item) => item.data_valid);
  if (valid.length) return `${valid.length} valid pairs · none qualified`;
  return `${current.length} comparisons · data incomplete or blocked`;
}

export function isStreamOff(status: StreamStatus | null | undefined): boolean {
  if (!status) return true;
  return !status.enabled && status.connection_status === "not_selected";
}

export function formatStreamDuration(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms >= 10_000 ? 0 : 1)} s`;
}

export function streamDataLabel(candidate: StreamCandidateStatus): string {
  if (candidate.current === false) return "cached";
  if (candidate.data_valid) return "valid";
  return "invalid";
}

export function streamEconomicsLabel(candidate: StreamCandidateStatus): string {
  if (candidate.economics_state === "qualified") return "qualified";
  if (candidate.economics_state === "no_arb") return "no arb";
  return "blocked";
}

export function streamPrice2Label(candidate: StreamCandidateStatus): string {
  if (candidate.paper_entry) return "paper fill";
  if (candidate.price2) return "accepted";
  return "none";
}

export function streamTopReason(candidate: StreamCandidateStatus): string {
  if (candidate.top_reason) return humanReason(candidate.top_reason);
  const first = candidate.rejection_reasons[0];
  return first ? humanReason(first) : "—";
}

export function humanReason(reason: string): string {
  const map: Record<string, string> = {
    "unknown_required_venue_cost:polymarket": "missing PM fee",
    "missing_venue_cost:matchbook": "missing MB fee",
    unknown_costs: "unknown costs",
    "missing_fx_rate:USD": "FX unavailable",
    stream_constituent_stale: "PM book stale",
    incomplete_stream_or_matchbook_pair: "incomplete books",
    future_token_timestamp: "future token clock",
    valid_no_arb: "valid, no arb",
    net_edge_below_threshold: "below min net",
    no_arbitrage: "no arb",
    canonical_identity_unavailable: "identity unavailable",
  };
  if (map[reason]) return map[reason];
  if (reason.startsWith("unknown_required_venue_cost:")) return `missing ${reason.split(":")[1]} fee`;
  if (reason.startsWith("missing_venue_cost:")) return `missing ${reason.split(":")[1]} fee`;
  if (reason.startsWith("missing_fx_rate:")) return `FX unavailable (${reason.split(":")[1]})`;
  return reason.replaceAll("_", " ");
}

export function streamRibbon(status: StreamStatus | null | undefined): StreamRibbonItem[] {
  const tokenCount = status?.token_id_count ?? 0;
  const registered = status?.subscribed_market_count ?? 0;
  const populated = status?.populated_pm_book_count ?? 0;
  const mbQuotes = status?.coalesced_matchbook_quote_count ?? 0;
  const booksCurrent = Boolean(status?.books_current);
  return [
    {
      key: "pm",
      label: "PM books",
      value: `${populated}/${tokenCount}`,
      tone: booksCurrent ? "ok" : "muted",
    },
    {
      key: "mb",
      label: "MB quotes",
      value: `${mbQuotes}/${registered}`,
      tone: booksCurrent && mbQuotes > 0 ? "ok" : "muted",
    },
    {
      key: "valid",
      label: "Valid pairs",
      value: String(status?.valid_pair_count ?? 0),
    },
    {
      key: "arbs",
      label: "Potential arbs",
      value: String(status?.potential_arb_count ?? 0),
    },
    {
      key: "p2",
      label: "Price-2",
      value: `${status?.price2_attempt_count ?? 0} / ${status?.price2_accepted_count ?? 0}`,
    },
    {
      key: "fills",
      label: "Paper fills",
      value: String(status?.paper_fill_count ?? (status?.paper_opened ? 1 : 0)),
    },
  ];
}
