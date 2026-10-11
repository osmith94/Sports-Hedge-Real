import type { OpportunityLifecycleEvent, Price2ActivityObservation } from "./api";

// Sounds are UI-only and never affect market data, pricing, or execution decisions.
export type SoundCue = "arb_detected" | "price2_accepted" | "price2_failed" | "trade_entered";

export type AudibleEvent = {
  id: string;
  at: number;
  cue: SoundCue;
};

const IMPORTANT_LIFECYCLE: Partial<Record<OpportunityLifecycleEvent["event_type"], SoundCue>> = {
  qualifying_detected: "arb_detected",
  paper_fill_complete: "trade_entered",
};

export const SOUND_FILES: Readonly<Record<SoundCue, string>> = {
  arb_detected: "gta-here-we-go-again",
  price2_accepted: "gta-san-andreas",
  price2_failed: "big-smoke-train-cj",
  trade_entered: "grand-theft-auto-san-andreas-mission-pass",
};

export const SOUND_POLL_MS = 8_000;
export const SOUND_LOOKBACK_MS = 120_000;
export const SOUND_QUEUE_LIMIT = 8;

export function audibleEvents(
  lifecycle: readonly OpportunityLifecycleEvent[],
  price2: readonly Price2ActivityObservation[],
): AudibleEvent[] {
  const events: AudibleEvent[] = [];
  for (const item of lifecycle) {
    const cue = IMPORTANT_LIFECYCLE[item.event_type];
    const at = Date.parse(item.occurred_at);
    if (cue && item.event_id && Number.isFinite(at)) {
      events.push({ id: `lifecycle:${item.event_id}`, at, cue });
    }
  }
  for (const item of price2) {
    const cue =
      item.status === "accepted"
        ? "price2_accepted"
        : item.status === "rejected" || item.status === "incomplete_unavailable"
          ? "price2_failed"
          : null;
    const at = Date.parse(item.occurred_at);
    if (cue && item.observation_id && Number.isFinite(at)) {
      events.push({ id: `price2:${item.observation_id}`, at, cue });
    }
  }
  return events.sort((a, b) => a.at - b.at || a.id.localeCompare(b.id));
}

/**
 * Deduplicate durable audit IDs, ignore pre-enable history, and avoid a sound
 * avalanche from multi-event polls. Distinct milestones get their own cues.
 */
export class SoundEventTracker {
  private seen = new Map<string, number>();

  constructor(private readonly enabledAt: number) {}

  consume(events: readonly AudibleEvent[]): SoundCue[] {
    const newEvents: AudibleEvent[] = [];
    const newest = Math.max(this.enabledAt, ...events.map((event) => event.at));
    // Keep only IDs that can still reappear in the bounded two-minute query.
    // Prevent an all-day operator session from accumulating unbounded IDs.
    for (const [id, when] of this.seen) {
      if (when < newest - SOUND_LOOKBACK_MS) this.seen.delete(id);
    }
    for (const event of events) {
      if (event.at < this.enabledAt || this.seen.has(event.id)) continue;
      this.seen.set(event.id, event.at);
      newEvents.push(event);
    }
    // Do not play multiple copies of the same sound from one scanner burst.
    const queued = new Set<SoundCue>();
    for (const event of newEvents.sort((a, b) => a.at - b.at || a.id.localeCompare(b.id))) {
      queued.add(event.cue);
    }
    // Play distinct milestones in the chronological order they were recorded.
    return [...queued];
  }
}
