import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { OpportunityLifecycleEvent } from "./api";
import {
  activityFromWatchlist,
  isOperatorActivityEvent,
  OPERATOR_ACTIVITY_EVENT_TYPES,
} from "./watchlist";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function event(
  overrides: Partial<OpportunityLifecycleEvent> &
    Pick<OpportunityLifecycleEvent, "event_id" | "event_type" | "occurred_at">,
): OpportunityLifecycleEvent {
  return {
    opportunity_id: "watch:mkt-1",
    status: "WATCHING",
    current_net_edge: "0.008",
    distance_to_trigger_pp: "0.2",
    detail: "fixture detail",
    ...overrides,
  };
}

describe("signal-only Activity feed", () => {
  it("renders only the four operator-significant titles", () => {
    const items = activityFromWatchlist([
      event({
        event_id: "noise-1",
        event_type: "candidate_first_seen",
        occurred_at: "2026-09-20T13:00:00Z",
      }),
      event({
        event_id: "noise-2",
        event_type: "moved_closer_to_trigger",
        occurred_at: "2026-09-20T13:00:01Z",
      }),
      event({
        event_id: "noise-3",
        event_type: "moved_further_from_trigger",
        occurred_at: "2026-09-20T13:00:02Z",
      }),
      event({
        event_id: "noise-4",
        event_type: "trigger_crossed",
        occurred_at: "2026-09-20T13:00:03Z",
      }),
      event({
        event_id: "noise-5",
        event_type: "paper_fill_attempted",
        occurred_at: "2026-09-20T13:00:04Z",
      }),
      event({
        event_id: "noise-6",
        event_type: "rejected_semantics",
        occurred_at: "2026-09-20T13:00:05Z",
      }),
      event({
        event_id: "hot-1",
        event_type: "promoted_to_hot",
        occurred_at: "2026-09-20T13:00:06Z",
        detail: "Brentford v Chelsea · BACKGROUND → HOT",
      }),
      event({
        event_id: "lost-1",
        event_type: "trigger_lost_before_fill",
        occurred_at: "2026-09-20T13:00:07Z",
      }),
      event({
        event_id: "enter-1",
        event_type: "paper_fill_complete",
        occurred_at: "2026-09-20T13:00:08Z",
      }),
      event({
        event_id: "exit-1",
        event_type: "closed",
        occurred_at: "2026-09-20T13:00:09Z",
      }),
    ]);
    assert.deepEqual(
      items.map((item) => item.title),
      ["Promoted to HOT", "Trigger lost before fill", "Trade entered", "Trade exited"],
    );
    assert.deepEqual(
      items.map((item) => item.kind),
      ["PROMOTED_TO_HOT", "TRIGGER_LOST_BEFORE_FILL", "TRADE_ENTERED", "TRADE_EXITED"],
    );
    assert.equal(items[1]?.missedTriggerEventId, "lost-1");
    assert.equal(items[2]?.missedTriggerEventId, null);
    assert.equal(items.find((item) => item.title === "Trigger lost before fill")?.title.includes("Trade"), false);
  });

  it("keeps one trade-entered card for a completed multi-leg paper fill", () => {
    const items = activityFromWatchlist([
      event({
        event_id: "watch:mkt-1:paper_fill_attempted:a1",
        event_type: "paper_fill_attempted",
        occurred_at: "2026-09-20T13:00:01Z",
      }),
      event({
        event_id: "watch:mkt-1:paper_fill_complete:a1",
        event_type: "paper_fill_complete",
        occurred_at: "2026-09-20T13:00:02Z",
        detail: "attempt_id=a1; paper_mode_only",
      }),
    ]);
    assert.equal(items.length, 1);
    assert.equal(items[0]?.title, "Trade entered");
    assert.equal(items[0]?.id, "watch:mkt-1:paper_fill_complete:a1");
  });

  it("preserves newest-first chronology after filtering noise", () => {
    const items = activityFromWatchlist([
      event({
        event_id: "later",
        event_type: "closed",
        occurred_at: "2026-09-20T13:00:09Z",
      }),
      event({
        event_id: "noise",
        event_type: "rejected_semantics",
        occurred_at: "2026-09-20T13:00:08Z",
      }),
      event({
        event_id: "earlier",
        event_type: "trigger_lost_before_fill",
        occurred_at: "2026-09-20T13:00:07Z",
      }),
    ]);
    assert.deepEqual(
      items.map((item) => item.id),
      ["later", "earlier"],
    );
  });

  it("operator console requests the signal-only feed and keeps #394 trigger-lost identity", () => {
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const feed = readFileSync(join(frontendRoot, "components/activity-feed.tsx"), "utf8");
    const api = readFileSync(join(frontendRoot, "lib/api.ts"), "utf8");
    assert.match(page, /operator_signal=true/);
    assert.match(feed, /Promoted to HOT/);
    assert.match(feed, /data-missed-trigger-event-id/);
    assert.doesNotMatch(feed, /Paper watchlist, threshold, fill and rejection events/);
    assert.match(api, /promoted_to_hot/);
    assert.deepEqual([...OPERATOR_ACTIVITY_EVENT_TYPES], [
      "promoted_to_hot",
      "trigger_lost_before_fill",
      "paper_fill_complete",
      "closed",
    ]);
    assert.equal(isOperatorActivityEvent("rejected_semantics"), false);
    assert.equal(isOperatorActivityEvent("paper_fill_attempted"), false);
    assert.equal(isOperatorActivityEvent("promoted_to_hot"), true);
  });
});
