import { OpportunityLifecycleEvent } from "./api";

export const NO_LATER_FILL_REJECTION_RECORDED =
  "No later fill/rejection event has been recorded yet";

export const TRIGGER_LOST_WITHOUT_FILL_ATTEMPT =
  "No durable fill attempt was recorded and the trigger was later lost.";

export function sortLifecycleChronological(
  events: OpportunityLifecycleEvent[],
): OpportunityLifecycleEvent[] {
  return [...events].sort((left, right) => {
    const byTime = left.occurred_at.localeCompare(right.occurred_at);
    if (byTime !== 0) return byTime;
    return left.event_id.localeCompare(right.event_id);
  });
}

export function noFillHistorySummary(
  events: OpportunityLifecycleEvent[],
): string | null {
  const chronological = sortLifecycleChronological(events);
  const paperEligible = chronological.some((event) => event.event_type === "paper_eligible");
  const tradeEntered = chronological.some((event) => event.event_type === "paper_fill_complete");
  if (!paperEligible || tradeEntered) {
    return null;
  }

  const rejected = [...chronological]
    .reverse()
    .find((event) => event.event_type === "paper_fill_rejected");
  if (rejected) {
    const detail = rejected.detail?.trim();
    return detail || "Paper fill was attempted and later rejected.";
  }

  const attempted = chronological.some((event) => event.event_type === "paper_fill_attempted");
  const triggerLost = chronological.some(
    (event) => event.event_type === "trigger_lost_before_fill",
  );
  if (!attempted && triggerLost) {
    return TRIGGER_LOST_WITHOUT_FILL_ATTEMPT;
  }

  return NO_LATER_FILL_REJECTION_RECORDED;
}
