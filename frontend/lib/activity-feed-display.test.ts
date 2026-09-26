import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { OpportunityLifecycleEvent } from "./api";
import {
  activityFromWatchlist,
  activityHistoryPath,
  activitySubjectFromEvent,
  isOperatorActivityEvent,
  isVisibleOperatorActivityEvent,
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
    fixture_label: "Brentford v Chelsea",
    market_family: "both_teams_to_score",
    canonical_event_id: "evt-signal",
    canonical_market_id: "mkt-1",
    ...overrides,
  };
}

describe("signal-only Activity feed", () => {
  it("renders only the five operator-significant titles", () => {
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
        event_id: "eligible-1",
        event_type: "paper_eligible",
        occurred_at: "2026-09-20T13:00:06Z",
        status: "TRIGGERED",
        capture_eligible: true,
        detail: "capture_eligible_triggered",
      }),
      event({
        event_id: "lost-1",
        event_type: "trigger_lost_before_fill",
        occurred_at: "2026-09-20T13:00:07Z",
        capture_eligible: true,
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
      [
        "Promoted to HOT",
        "Paper eligible",
        "Trigger lost before fill",
        "Trade entered",
        "Trade exited",
      ],
    );
    assert.deepEqual(
      items.map((item) => item.kind),
      [
        "PROMOTED_TO_HOT",
        "PAPER_ELIGIBLE",
        "TRIGGER_LOST_BEFORE_FILL",
        "TRADE_ENTERED",
        "TRADE_EXITED",
      ],
    );
    assert.equal(items[2]?.missedTriggerEventId, "lost-1");
    assert.equal(items[3]?.missedTriggerEventId, null);
    assert.equal(items.find((item) => item.title === "Trigger lost before fill")?.title.includes("Trade"), false);
    assert.ok(items.every((item) => item.subject === "Brentford v Chelsea · both teams to score"));
    assert.ok(items.every((item) => item.fixtureLabel === "Brentford v Chelsea"));
    assert.ok(items.every((item) => item.marketFamily === "both_teams_to_score"));
    assert.ok(items.every((item) => item.opportunityId === "watch:mkt-1"));
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
        capture_eligible: true,
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
    assert.match(
      feed,
      /Qualifying opportunity, radar expired, Promoted to HOT, paper eligible, trigger lost, trade entered, trade exited/,
    );
    assert.match(feed, /data-missed-trigger-event-id/);
    assert.match(feed, /feed-subject/);
    assert.match(feed, /data-fixture-label/);
    assert.match(feed, /data-history-opportunity-id/);
    assert.match(feed, /activityHistoryPath/);
    assert.doesNotMatch(feed, /Paper watchlist, threshold, fill and rejection events/);
    assert.match(api, /promoted_to_hot/);
    assert.match(api, /paper_eligible/);
    assert.deepEqual([...OPERATOR_ACTIVITY_EVENT_TYPES], [
      "promoted_to_hot",
      "qualifying_detected",
      "qualifying_lost",
      "qualifying_expired",
      "paper_eligible",
      "trigger_lost_before_fill",
      "paper_fill_complete",
      "closed",
    ]);
    assert.equal(isOperatorActivityEvent("rejected_semantics"), false);
    assert.equal(isOperatorActivityEvent("paper_fill_attempted"), false);
    assert.equal(isOperatorActivityEvent("trigger_crossed"), false);
    assert.equal(isOperatorActivityEvent("promoted_to_hot"), true);
    assert.equal(isOperatorActivityEvent("paper_eligible"), true);
    assert.equal(isOperatorActivityEvent("qualifying_detected"), true);
    assert.equal(isOperatorActivityEvent("qualifying_lost"), true);
    assert.equal(isOperatorActivityEvent("qualifying_expired"), true);
    assert.equal(isOperatorActivityEvent("expired"), false);
    assert.equal(activityHistoryPath("watch:mkt-1"), "/activity/watch%3Amkt-1");
    assert.equal(
      activityHistoryPath("watch:mkt-1", "evt-signal"),
      "/activity/watch%3Amkt-1?canonical_event_id=evt-signal",
    );
    assert.equal(
      activityHistoryPath("hot:evt-signal"),
      "/activity/hot%3Aevt-signal?canonical_event_id=evt-signal",
    );
  });

  it("shows fixture and market at a glance from durable event metadata", () => {
    const items = activityFromWatchlist([
      event({
        event_id: "hot-1",
        event_type: "promoted_to_hot",
        occurred_at: "2026-09-20T13:00:06Z",
        detail: "Brentford v Chelsea · both teams to score · BACKGROUND → HOT",
      }),
    ]);
    assert.equal(items[0]?.subject, "Brentford v Chelsea · both teams to score");
    assert.equal(items[0]?.detail, "BACKGROUND → HOT");
    assert.equal(
      activitySubjectFromEvent(
        event({
          event_id: "enter-1",
          event_type: "paper_fill_complete",
          occurred_at: "2026-09-20T13:00:08Z",
        }),
      ),
      "Brentford v Chelsea · both teams to score",
    );
  });

  it("hides economic-only trigger-lost and never treats trigger_crossed as a missed fill", () => {
    const items = activityFromWatchlist([
      event({
        event_id: "cross-1",
        event_type: "trigger_crossed",
        occurred_at: "2026-09-20T13:00:03Z",
        capture_eligible: true,
      }),
      event({
        event_id: "econ-lost",
        event_type: "trigger_lost_before_fill",
        occurred_at: "2026-09-20T13:00:07Z",
        capture_eligible: false,
      }),
      event({
        event_id: "eligible-lost",
        event_type: "trigger_lost_before_fill",
        occurred_at: "2026-09-20T13:00:08Z",
        capture_eligible: true,
      }),
    ]);
    assert.deepEqual(
      items.map((item) => item.id),
      ["eligible-lost"],
    );
    assert.equal(items[0]?.title, "Trigger lost before fill");
    assert.equal(items[0]?.kind, "TRIGGER_LOST_BEFORE_FILL");
    assert.equal(
      isVisibleOperatorActivityEvent(
        event({
          event_id: "econ-lost",
          event_type: "trigger_lost_before_fill",
          occurred_at: "2026-09-20T13:00:07Z",
          capture_eligible: false,
        }),
      ),
      false,
    );
    assert.equal(
      isVisibleOperatorActivityEvent(
        event({
          event_id: "cross-1",
          event_type: "trigger_crossed",
          occurred_at: "2026-09-20T13:00:03Z",
          capture_eligible: true,
        }),
      ),
      false,
    );
    assert.equal(
      isVisibleOperatorActivityEvent(
        event({
          event_id: "eligible-1",
          event_type: "paper_eligible",
          occurred_at: "2026-09-20T13:00:03Z",
          status: "TRIGGERED",
          capture_eligible: true,
        }),
      ),
      true,
    );
  });

  it("renders a qualifying opportunity separately from paper eligible", () => {
    const items = activityFromWatchlist([
      event({
        event_id: "qualify-1",
        event_type: "qualifying_detected",
        occurred_at: "2026-09-20T12:00:00Z",
        status: "TRIGGERED",
        capture_eligible: false,
        fixture_label: "North Macedonia v Switzerland",
        market_family: "total_goals",
        current_net_edge: "0.0363",
        gross_edge: "0.0492",
        limiting_depth_gbp: "206.91",
        guaranteed_profit_gbp: "19.73",
        venue_pair: "matchbook,kalshi",
        pricing_lane: "background",
        quote_age_ms: 800,
        detail: "solver_qualified",
      }),
      event({
        event_id: "eligible-1",
        event_type: "paper_eligible",
        occurred_at: "2026-09-20T12:00:01Z",
        status: "TRIGGERED",
        capture_eligible: true,
        fixture_label: "North Macedonia v Switzerland",
        market_family: "total_goals",
        detail: "capture_eligible_triggered",
      }),
      event({
        event_id: "lost-econ",
        event_type: "qualifying_lost",
        occurred_at: "2026-09-20T12:01:00Z",
        capture_eligible: false,
        fixture_label: "North Macedonia v Switzerland",
        market_family: "total_goals",
        detail: "qualifying lost · net edge below threshold",
      }),
      event({
        event_id: "enter-1",
        event_type: "paper_fill_complete",
        occurred_at: "2026-09-20T12:02:00Z",
        fixture_label: "North Macedonia v Switzerland",
        market_family: "total_goals",
      }),
    ]);
    assert.deepEqual(
      items.map((item) => item.title),
      ["Qualifying opportunity", "Paper eligible", "Qualifying lost", "Trade entered"],
    );
    assert.deepEqual(
      items.map((item) => item.kind),
      ["QUALIFYING_OPPORTUNITY", "PAPER_ELIGIBLE", "QUALIFYING_LOST", "TRADE_ENTERED"],
    );
    assert.equal(items[0]?.kind.replaceAll("_", " "), "QUALIFYING OPPORTUNITY");
    assert.equal(items[0]?.subject, "North Macedonia v Switzerland · total goals");
    assert.equal(
      items[0]?.detail,
      "MB / K · gross 4.92% · net 3.63% · executable £206.91 · guaranteed £19.73 · BACKGROUND pricing",
    );
    assert.equal(items[1]?.title, "Paper eligible");
    assert.equal(items[2]?.detail, "qualifying lost · net edge below threshold");
    assert.equal(items[3]?.title, "Trade entered");
  });
});
