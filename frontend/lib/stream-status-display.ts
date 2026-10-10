import type { StreamStatus } from "./api";

export const STREAM_HEADING = "STREAM";
export const STREAM_KICKER = "Shadow surveillance · Phase 1";
export const STREAM_COPY =
  "One manually pinned fixture. Public Polymarket market-channel observer plus coalesced Matchbook exact-ID refresh. Candidate edges are telemetry, not Price-2 and not paper entry.";
export const STREAM_EMPTY = "No fixture selected. STREAM stays off until Send to STREAM.";
export const STREAM_SHADOW_LABEL = "SHADOW ONLY · NOT EXECUTABLE · NOT PRICE-2";
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
  return `${trustworthy.length} trustworthy diagnostic candidate${trustworthy.length === 1 ? "" : "s"} · still not executable.`;
}

export function isStreamOff(status: StreamStatus | null | undefined): boolean {
  if (!status) return true;
  return !status.enabled && status.connection_status === "not_selected";
}
