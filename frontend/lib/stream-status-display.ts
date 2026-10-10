import type { StreamStatus } from "./api";

export const STREAM_HEADING = "STREAM";
export const STREAM_KICKER = "STREAM · Price-2 handoff · execution disarmed";
export const STREAM_COPY =
  "One manually pinned fixture. Populated Polymarket native-token books plus coalesced Matchbook exact-ID depth feed the existing Price-2 and paper opening path. Execution stays disarmed unless mode is real and SPORTS_HEDGE_EXECUTION_ENABLED is set. Empty WS [] is not a pass.";
export const STREAM_EMPTY = "No fixture selected. STREAM stays off until Send to STREAM.";
export const STREAM_SHADOW_LABEL = "PRICE-2 HANDOFF · EXECUTION DISARMED";
export const SEND_TO_STREAM_LABEL = "Send to STREAM";

export function streamStatusLabel(status: StreamStatus | null | undefined): string {
  const raw = String(status?.connection_status || "not_selected").replaceAll("_", " ");
  return raw.toUpperCase();
}

export function streamFixtureName(status: StreamStatus | null | undefined): string {
  if (!status?.canonical_event_id) return STREAM_EMPTY;
  const home = status.home_team || "Home";
  const away = status.away_team || "Away";
  return `${home} v ${away}`;
}

export function streamCandidateSummary(status: StreamStatus | null | undefined): string {
  const candidates = status?.candidates ?? [];
  if (!candidates.length) return "No STREAM candidate edges.";
  const trustworthy = candidates.filter((item) => item.trustworthy);
  if (!trustworthy.length) {
    return `${candidates.length} diagnostic comparisons · none marked trustworthy.`;
  }
  const price2 = candidates.filter((item) => item.price2);
  if (price2.length) {
    return `${price2.length} Price-2 accepted candidate${price2.length === 1 ? "" : "s"} · execution still disarmed unless separately armed.`;
  }
  return `${trustworthy.length} trustworthy diagnostic candidate${trustworthy.length === 1 ? "" : "s"} · waiting for existing Price-2.`;
}

export function isStreamOff(status: StreamStatus | null | undefined): boolean {
  if (!status) return true;
  return !status.enabled && status.connection_status === "not_selected";
}
