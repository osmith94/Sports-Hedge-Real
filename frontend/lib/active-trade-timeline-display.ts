import { ActiveTradeTimelineItem } from "./api";

export function activeTradeTimelineLines(
  items: ActiveTradeTimelineItem[] | null | undefined,
): string[] {
  if (!items || items.length === 0) {
    return ["ACTIVE TRADE journal · no persisted events yet"];
  }
  return items.map((item) => {
    const when = item.occurred_at.replace("T", " ").slice(0, 19);
    const venue = item.venue ? ` · ${item.venue}` : "";
    return `${when}Z · ${item.event_type} · ${item.reason_code}${venue} · ${item.operator_copy}`;
  });
}
