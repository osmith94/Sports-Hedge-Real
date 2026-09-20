import { ActiveTradeJournalEvent, ActiveTradeTimelineItem } from "./api";

const EVENT_QUESTIONS: Record<string, string> = {
  promoted_to_active: "How did this enter ACTIVE TRADE?",
  active_refresh_started: "What happened on this 5s cycle?",
  active_refresh_result: "What happened on this 5s cycle?",
  entry_decision: "Why did we buy?",
  entry_attempt: "Why did we buy?",
  entry_fill: "Why did we buy?",
  entry_partial_fill: "Where did a partial fill occur?",
  entry_recovery_decision: "What recovery was attempted?",
  entry_recovery_fill: "What recovery was attempted?",
  entry_recovery_residual: "What recovery was attempted?",
  topup_decision: "Why did we top up?",
  topup_attempt: "Why did we top up?",
  topup_fill: "Why did we top up?",
  topup_partial_fill: "Where did a partial fill occur?",
  cap_reached: "Why didn't we top up?",
  below_min_net_arb: "Why didn't we top up?",
  no_action: "Why didn't we top up?",
  unwind_decision: "Position-management observation",
  settled: "How did the trade finish?",
  closed: "How did the trade finish?",
  journal_write_failed: "Could this cycle be recorded?",
};

const PAYLOAD_LABELS: Array<[string, string]> = [
  ["net_edge", "net edge"],
  ["current_net", "current net"],
  ["min_net", "min net arb"],
  ["trigger", "min net arb"],
  ["requested_gbp", "requested"],
  ["incremental_gbp", "incremental"],
  ["filled_stake", "filled"],
  ["residual_gbp", "residual"],
  ["treasury_before_gbp", "treasury before"],
  ["treasury_after_gbp", "treasury after"],
  ["realised_pnl_gbp", "realised P&L"],
  ["settlement_outcome", "settlement"],
  ["status", "refresh status"],
  ["last_persist_error", "persist error"],
  ["size_reason", "size reason"],
];

export const ACTIVE_TRADE_LOG_EMPTY = "Trade log · no persisted ACTIVE TRADE events yet";

export function activeTradeEventQuestion(eventType: string | null | undefined): string {
  if (!eventType) return "What happened?";
  return EVENT_QUESTIONS[eventType] ?? "What happened?";
}

export function activeTradePayloadBits(
  payload: Record<string, unknown> | null | undefined,
): string | null {
  if (!payload) return null;
  const bits: string[] = [];
  const seen = new Set<string>();
  for (const [key, label] of PAYLOAD_LABELS) {
    const value = payload[key];
    if (value == null || value === "") continue;
    if (seen.has(label)) continue;
    seen.add(label);
    bits.push(`${label} ${String(value)}`);
  }
  if (payload.native_ids) bits.push("native IDs recorded");
  if (payload.books) bits.push("executable books recorded");
  return bits.length ? bits.join(" · ") : null;
}

export function activeTradeTimelineLines(
  items: Array<ActiveTradeTimelineItem | ActiveTradeJournalEvent> | null | undefined,
): string[] {
  if (!items || items.length === 0) {
    return [ACTIVE_TRADE_LOG_EMPTY];
  }
  return items.map((item) => {
    const when = item.occurred_at.replace("T", " ").slice(0, 19);
    const venue = item.venue ? ` · ${item.venue}` : "";
    const payload =
      "payload" in item ? activeTradePayloadBits(item.payload ?? undefined) : null;
    const extra = payload ? ` · ${payload}` : "";
    return `${when}Z · ${item.event_type} · ${item.reason_code}${venue} · ${item.operator_copy}${extra}`;
  });
}
