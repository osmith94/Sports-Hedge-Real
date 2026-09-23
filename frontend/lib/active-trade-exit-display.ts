import { PaperTrade } from "./api";
import { number, percent, percentPoints } from "./format";

export function formatSignedPercent(value: string | number | null | undefined): string {
  const parsed = number(value);
  if (parsed === null) return "—";
  const body = percent(Math.abs(parsed));
  if (parsed > 0) return `+${body}`;
  if (parsed < 0) return `-${body}`;
  return body;
}

export function formatDeltaPp(value: string | number | null | undefined): string {
  const parsed = number(value);
  if (parsed === null) return "—";
  const body = percentPoints(Math.abs(parsed));
  if (parsed > 0) return `+${body}`;
  if (parsed < 0) return `-${body}`;
  return body;
}

export function formatEntryArb(trade: PaperTrade): string {
  return formatSignedPercent(trade.entry_net_edge);
}

export function formatCurrentExit(trade: PaperTrade): string {
  if (trade.current_exit_pct == null || trade.current_exit_pct === "") return "—";
  return formatSignedPercent(trade.current_exit_pct);
}

export function formatExitDelta(trade: PaperTrade): string {
  if (trade.current_exit_pct == null || trade.entry_net_edge == null) return "—";
  return formatDeltaPp(trade.current_exit_delta_pp);
}

export function exitBlockReasonText(trade: PaperTrade): string | null {
  const reason = trade.current_exit_block_reason;
  if (!reason) return null;
  return reason.replaceAll("_", " ");
}
