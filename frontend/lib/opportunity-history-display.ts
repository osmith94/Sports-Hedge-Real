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
    const leftSeq = left.append_seq;
    const rightSeq = right.append_seq;
    if (leftSeq != null && rightSeq != null && leftSeq !== rightSeq) {
      return leftSeq - rightSeq;
    }
    return 0;
  });
}

export function noFillHistorySummary(
  events: OpportunityLifecycleEvent[],
  opportunityId?: string | null,
): string | null {
  const chronological = sortLifecycleChronological(events);
  const scoped = opportunityId
    ? chronological.filter((event) => event.opportunity_id === opportunityId)
    : chronological;
  let lastEligible = -1;
  for (let index = 0; index < scoped.length; index += 1) {
    if (scoped[index]?.event_type === "paper_eligible") {
      lastEligible = index;
    }
  }
  if (lastEligible < 0) {
    return null;
  }
  const episode = scoped.slice(lastEligible + 1);
  if (episode.some((event) => event.event_type === "paper_fill_complete")) {
    return null;
  }

  const rejected = [...episode]
    .reverse()
    .find((event) => event.event_type === "paper_fill_rejected");
  if (rejected) {
    const detail = rejected.detail?.trim();
    return detail || "Paper fill was attempted and later rejected.";
  }

  const attempted = episode.some((event) => event.event_type === "paper_fill_attempted");
  const triggerLost = episode.some(
    (event) => event.event_type === "trigger_lost_before_fill",
  );
  if (!attempted && triggerLost) {
    return TRIGGER_LOST_WITHOUT_FILL_ATTEMPT;
  }

  return NO_LATER_FILL_REJECTION_RECORDED;
}
