import { describe, expect, it } from "vitest";

import type { OpportunityLifecycleEvent, Price2ActivityObservation } from "./api";
import { SOUND_FILES, SoundEventTracker, audibleEvents } from "./sound-alerts";

const start = Date.parse("2026-10-11T18:00:00.000Z");

function lifecycle(
  event_type: OpportunityLifecycleEvent["event_type"],
  event_id: string,
  occurred_at = "2026-10-11T18:00:01.000Z",
): OpportunityLifecycleEvent {
  return {
    event_type,
    event_id,
    occurred_at,
    status: "TRIGGERED",
    opportunity_id: "watch:one",
  };
}

function price2(
  status: Price2ActivityObservation["status"],
  observation_id: string,
  occurred_at = "2026-10-11T18:00:02.000Z",
): Price2ActivityObservation {
  return {
    observation_id,
    opportunity_id: "watch:one",
    source: "execution_snapshot_audit",
    occurred_at,
    status,
    accepted: status === "accepted",
    filled: false,
    data_kind: "historical_recorded",
    legs: [],
  };
}

describe("GTA milestone sound alerts", () => {
  it("maps each requested milestone without confusing Price-2 accepted with a filled trade", () => {
    const rows = audibleEvents(
      [
        lifecycle("qualifying_detected", "qualify"),
        lifecycle("paper_fill_complete", "trade", "2026-10-11T18:00:03.000Z"),
        lifecycle("promoted_to_hot", "noise"),
      ],
      [
        price2("accepted", "accept"),
        price2("rejected", "fail", "2026-10-11T18:00:04.000Z"),
        price2("incomplete_unavailable", "missing", "2026-10-11T18:00:05.000Z"),
      ],
    );
    expect(rows.map(({ cue }) => cue)).toEqual([
      "arb_detected", "price2_accepted", "trade_entered", "price2_failed", "price2_failed",
    ]);
    expect(SOUND_FILES).toEqual({
      arb_detected: "gta-here-we-go-again",
      price2_accepted: "gta-san-andreas",
      price2_failed: "big-smoke-train-cj",
      trade_entered: "grand-theft-auto-san-andreas-mission-pass",
    });
  });

  it("never replays events that occurred before sound activation", () => {
    const tracker = new SoundEventTracker(start);
    const events = audibleEvents(
      [lifecycle("qualifying_detected", "old", "2026-10-11T17:59:59.000Z")],
      [],
    );
    expect(tracker.consume(events)).toEqual([]);
    expect(tracker.consume(events)).toEqual([]);
  });

  it("plays new events once, in milestone chronology, ignoring later identical polls", () => {
    const tracker = new SoundEventTracker(start);
    const events = audibleEvents(
      [
        lifecycle("qualifying_detected", "qualify"),
        lifecycle("paper_fill_complete", "trade", "2026-10-11T18:00:03.000Z"),
      ],
      [price2("accepted", "accepted")],
    );
    expect(tracker.consume(events)).toEqual([
      "arb_detected", "price2_accepted", "trade_entered",
    ]);
    expect(tracker.consume(events)).toEqual([]);
    expect(tracker.consume(audibleEvents([], [price2("rejected", "new", "2026-10-11T18:00:04.000Z")]))).toEqual([
      "price2_failed",
    ]);
  });

  it("coalesces repeated detections and failures within one polling burst", () => {
    const tracker = new SoundEventTracker(start);
    expect(tracker.consume(audibleEvents(
      [
        lifecycle("qualifying_detected", "q1"),
        lifecycle("qualifying_detected", "q2", "2026-10-11T18:00:02.000Z"),
      ],
      [
        price2("rejected", "r1", "2026-10-11T18:00:03.000Z"),
        price2("incomplete_unavailable", "r2", "2026-10-11T18:00:04.000Z"),
      ],
    ))).toEqual(["arb_detected", "price2_failed"]);
  });

  it("drops malformed timestamps and unrelated lifecycle changes", () => {
    expect(audibleEvents(
      [
        lifecycle("moved_closer_to_trigger", "not-a-qualifier"),
        lifecycle("qualifying_detected", "invalid", "not-a-date"),
      ],
      [],
    )).toEqual([]);
  });
});
