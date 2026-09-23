import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { OpportunityLifecycleEvent } from "./api";
import {
  NO_LATER_FILL_REJECTION_RECORDED,
  TRIGGER_LOST_WITHOUT_FILL_ATTEMPT,
  noFillHistorySummary,
  sortLifecycleChronological,
} from "./opportunity-history-display";
import { activityHistoryPath, attemptIdFromLifecycleEvent, lifecycleEventTitle } from "./watchlist";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function event(
  overrides: Partial<OpportunityLifecycleEvent> &
    Pick<OpportunityLifecycleEvent, "event_id" | "event_type" | "occurred_at">,
): OpportunityLifecycleEvent {
  return {
    opportunity_id: "watch:mkt-1",
    status: "TRIGGERED",
    current_net_edge: "0.012",
    distance_to_trigger_pp: "-0.2",
    detail: "capture_eligible_triggered",
    fixture_label: "Brentford v Chelsea",
    market_family: "both_teams_to_score",
    canonical_event_id: "evt-signal",
    canonical_market_id: "mkt-1",
    capture_eligible: true,
    ...overrides,
  };
}

describe("per-opportunity activity history", () => {
  it("uses the exact opportunity ID in the History path", () => {
    assert.equal(activityHistoryPath("watch:mkt-1"), "/activity/watch%3Amkt-1");
    const feed = readFileSync(join(frontendRoot, "components/activity-feed.tsx"), "utf8");
    assert.match(feed, /activityHistoryPath\(item\.opportunityId, item\.canonicalEventId\)/);
    assert.match(feed, /data-history-canonical-event-id/);
    assert.match(feed, /data-history-opportunity-id=\{item\.opportunityId\}/);
    const page = readFileSync(
      join(frontendRoot, "app/activity/[opportunityId]/page.tsx"),
      "utf8",
    );
    assert.match(page, /canonical_event_id=\$\{encodeURIComponent\(canonicalEventId\)\}/);
    assert.match(page, /canonicalEventIdFromHotOpportunityId/);
    assert.match(page, /data-canonical-event-id/);
    assert.match(page, /data-canonical-market-id/);
    assert.match(page, /data-attempt-id/);
    assert.match(page, /attemptIdFromLifecycleEvent/);
    assert.match(page, /noFillHistorySummary\(chronological, opportunityId\)/);
    assert.doesNotMatch(page, /operator_signal/);
  });

  it("renders hidden lifecycle titles and persisted details", () => {
    assert.equal(lifecycleEventTitle("paper_eligible"), "Paper eligible");
    assert.equal(lifecycleEventTitle("trigger_crossed"), "Threshold crossed");
    assert.equal(lifecycleEventTitle("paper_fill_attempted"), "Paper fill attempted");
    assert.equal(lifecycleEventTitle("paper_fill_rejected"), "Paper capture rejected");
    assert.equal(lifecycleEventTitle("trigger_lost_before_fill"), "Trigger lost before fill");
    const chronological = sortLifecycleChronological([
      event({
        event_id: "later",
        event_type: "paper_fill_rejected",
        occurred_at: "2026-09-20T13:00:04Z",
        status: "REJECTED",
        detail: "opening_leg_failed:kalshi",
      }),
      event({
        event_id: "earlier",
        event_type: "paper_eligible",
        occurred_at: "2026-09-20T13:00:01Z",
        detail: "capture_eligible_triggered",
      }),
      event({
        event_id: "mid",
        event_type: "paper_fill_attempted",
        occurred_at: "2026-09-20T13:00:03Z",
        status: "PAPER_FILLING",
        detail: "paper_fill_attempted_bound_snapshot",
      }),
    ]);
    assert.deepEqual(
      chronological.map((item) => item.event_id),
      ["earlier", "mid", "later"],
    );
    assert.equal(chronological[2]?.detail, "opening_leg_failed:kalshi");
  });

  it("derives no-fill summary only from persisted lifecycle events", () => {
    const eligible = event({
      event_id: "eligible-1",
      event_type: "paper_eligible",
      occurred_at: "2026-09-20T13:00:01Z",
    });
    assert.equal(noFillHistorySummary([eligible]), NO_LATER_FILL_REJECTION_RECORDED);
    assert.equal(
      noFillHistorySummary([
        eligible,
        event({
          event_id: "lost-1",
          event_type: "trigger_lost_before_fill",
          occurred_at: "2026-09-20T13:00:08Z",
          status: "WATCHING",
          detail: "trigger_lost_before_paper_fill",
        }),
      ]),
      TRIGGER_LOST_WITHOUT_FILL_ATTEMPT,
    );
    assert.equal(
      noFillHistorySummary([
        eligible,
        event({
          event_id: "attempt-1",
          event_type: "paper_fill_attempted",
          occurred_at: "2026-09-20T13:00:02Z",
          status: "PAPER_FILLING",
          detail: "paper_fill_attempted_bound_snapshot",
        }),
        event({
          event_id: "reject-1",
          event_type: "paper_fill_rejected",
          occurred_at: "2026-09-20T13:00:03Z",
          status: "REJECTED",
          detail: "treasury_lock_failed",
        }),
      ]),
      "treasury_lock_failed",
    );
    assert.equal(
      noFillHistorySummary([
        eligible,
        event({
          event_id: "enter-1",
          event_type: "paper_fill_complete",
          occurred_at: "2026-09-20T13:00:04Z",
          status: "FILLED",
          detail: "paper_mode_only",
        }),
      ]),
      null,
    );
    assert.equal(
      noFillHistorySummary([
        event({
          event_id: "cross-1",
          event_type: "trigger_crossed",
          occurred_at: "2026-09-20T13:00:01Z",
          capture_eligible: true,
        }),
      ]),
      null,
    );
  });

  it("scopes no-fill summary to the latest paper_eligible episode", () => {
    const eligible1 = event({
      event_id: "eligible-1",
      event_type: "paper_eligible",
      occurred_at: "2026-09-20T13:00:01Z",
    });
    const rejected1 = event({
      event_id: "watch:mkt-1:paper_fill_rejected:attempt-old",
      event_type: "paper_fill_rejected",
      occurred_at: "2026-09-20T13:00:02Z",
      status: "REJECTED",
      detail: "opening_leg_failed:kalshi",
      attempt_id: "attempt-old",
    });
    const lost1 = event({
      event_id: "lost-1",
      event_type: "trigger_lost_before_fill",
      occurred_at: "2026-09-20T13:00:03Z",
      status: "WATCHING",
    });
    const eligible2 = event({
      event_id: "eligible-2",
      event_type: "paper_eligible",
      occurred_at: "2026-09-20T13:00:10Z",
    });
    const filled1 = event({
      event_id: "watch:mkt-1:paper_fill_complete:attempt-old",
      event_type: "paper_fill_complete",
      occurred_at: "2026-09-20T13:00:02Z",
      status: "FILLED",
      attempt_id: "attempt-old",
    });
    assert.equal(
      noFillHistorySummary([eligible1, rejected1, lost1, eligible2], "watch:mkt-1"),
      NO_LATER_FILL_REJECTION_RECORDED,
    );
    assert.equal(
      noFillHistorySummary([eligible1, filled1, eligible2], "watch:mkt-1"),
      NO_LATER_FILL_REJECTION_RECORDED,
    );
    assert.equal(
      noFillHistorySummary(
        [
          eligible1,
          rejected1,
          eligible2,
          event({
            event_id: "lost-2",
            event_type: "trigger_lost_before_fill",
            occurred_at: "2026-09-20T13:00:12Z",
            status: "WATCHING",
          }),
        ],
        "watch:mkt-1",
      ),
      TRIGGER_LOST_WITHOUT_FILL_ATTEMPT,
    );
    assert.equal(noFillHistorySummary([eligible1, filled1], "watch:mkt-1"), null);
    assert.equal(
      noFillHistorySummary(
        [
          eligible1,
          rejected1,
          event({
            event_id: "eligible-other",
            opportunity_id: "watch:mkt-other",
            event_type: "paper_eligible",
            occurred_at: "2026-09-20T13:00:20Z",
            canonical_market_id: "mkt-other",
          }),
        ],
        "watch:mkt-1",
      ),
      "opening_leg_failed:kalshi",
    );
  });

  it("orders same-timestamp rows by append_seq instead of event_id", () => {
    const chronological = sortLifecycleChronological([
      event({
        event_id: "zzz-last-lexically",
        event_type: "paper_eligible",
        occurred_at: "2026-09-20T13:00:00Z",
        append_seq: 3,
      }),
      event({
        event_id: "aaa-first-lexically",
        event_type: "trigger_crossed",
        occurred_at: "2026-09-20T13:00:00Z",
        append_seq: 2,
      }),
      event({
        event_id: "mmm-mid-lexically",
        event_type: "candidate_first_seen",
        occurred_at: "2026-09-20T13:00:00Z",
        append_seq: 1,
      }),
    ]);
    assert.deepEqual(
      chronological.map((item) => item.event_type),
      ["candidate_first_seen", "trigger_crossed", "paper_eligible"],
    );
  });

  it("exposes attempt_id from the durable fill event identity", () => {
    assert.equal(
      attemptIdFromLifecycleEvent(
        event({
          event_id: "watch:mkt-1:paper_fill_attempted:attempt-9",
          event_type: "paper_fill_attempted",
          occurred_at: "2026-09-20T13:00:02Z",
          status: "PAPER_FILLING",
        }),
      ),
      "attempt-9",
    );
    assert.equal(
      attemptIdFromLifecycleEvent(
        event({
          event_id: "watch:mkt-1:paper_fill_complete:attempt-9",
          event_type: "paper_fill_complete",
          occurred_at: "2026-09-20T13:00:03Z",
          status: "FILLED",
          attempt_id: "attempt-9",
        }),
      ),
      "attempt-9",
    );
    assert.equal(
      attemptIdFromLifecycleEvent(
        event({
          event_id: "uuid-eligible",
          event_type: "paper_eligible",
          occurred_at: "2026-09-20T13:00:01Z",
        }),
      ),
      null,
    );
  });
});
