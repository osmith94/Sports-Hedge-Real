import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { OpportunityLifecycleEvent, Price2ActivityObservation } from "./api";
import { formatDateLocalClockWithMs, formatLocalClockWithMs } from "./format";
import {
  activityFromPrice2,
  activityFromWatchlist,
  mergeOperatorActivity,
  oldestVisibleOccurredAt,
  price2CompactDetail,
  price2ActivityQuery,
  price2TimingLine,
  price2Title,
  visibleOpportunityIds,
} from "./watchlist";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function lifecycle(
  overrides: Partial<OpportunityLifecycleEvent> &
    Pick<OpportunityLifecycleEvent, "event_id" | "event_type" | "occurred_at">,
): OpportunityLifecycleEvent {
  return {
    opportunity_id: "watch:mkt-moreirense",
    status: "TRIGGERED",
    current_net_edge: "0.6631",
    fixture_label: "Moreirense FC v Gil Vicente",
    market_family: "match_result",
    canonical_event_id: "evt-moreirense",
    canonical_market_id: "mkt-moreirense",
    ...overrides,
  };
}

function attempt(
  overrides: Partial<Price2ActivityObservation> = {},
): Price2ActivityObservation {
  return {
    observation_id: "exec:accepted-1",
    source: "execution_snapshot_audit",
    opportunity_id: "watch:mkt-moreirense",
    snapshot_id: "exec:accepted-1",
    execution_cycle: 1,
    cycle_outcome: "accepted",
    occurred_at: "2026-10-09T19:11:42.521Z",
    started_at: "2026-10-09T19:11:41.709Z",
    finished_at: "2026-10-09T19:11:42.521Z",
    elapsed_ms: 812,
    status: "accepted",
    accepted: true,
    filled: false,
    trade_linked: false,
    net_edge: "0.012",
    guaranteed_profit: "0.04",
    execution_size: "3",
    execution_size_currency: "GBP",
    oldest_quote_age_ms: 180,
    skew_ms: 40,
    fixture_label: "Moreirense FC v Gil Vicente",
    market_family: "match_result",
    legs: [
      {
        venue: "matchbook",
        outcome: "home",
        displayed_odds: "2.04",
        requested_stake: "3",
        stake_currency: "GBP",
        retrieved_at: "2026-10-09T19:11:41.709Z",
        quote_age_ms: 120,
        slot_wait_ms: 12,
        io_ms: 40,
      },
    ],
    data_kind: "historical_recorded",
    ...overrides,
  };
}

describe("Price-2 activity feed projection", () => {
  it("keeps Price-1 qualifying lost independent of Price-2", () => {
    const lifecycleItems = activityFromWatchlist([
      lifecycle({
        event_id: "q1",
        event_type: "qualifying_detected",
        occurred_at: "2026-10-09T19:10:00.000Z",
      }),
      lifecycle({
        event_id: "lost",
        event_type: "qualifying_lost",
        occurred_at: "2026-10-09T19:12:00.000Z",
        detail: "qualifying lost · net edge below threshold",
      }),
    ]);
    const merged = mergeOperatorActivity(lifecycleItems, []);
    assert.deepEqual(
      merged.map((item) => item.title),
      ["Qualifying lost", "Qualifying opportunity"],
    );
    assert.equal(merged.some((item) => item.title.startsWith("Price-2")), false);
  });

  it("orders Price-2 by recorded timestamps and does not treat accepted as a fill", () => {
    const lifecycleItems = activityFromWatchlist([
      lifecycle({
        event_id: "q1",
        event_type: "qualifying_detected",
        occurred_at: "2026-10-09T19:10:00.000Z",
        current_net_edge: "0.6631",
      }),
    ]);
    const merged = mergeOperatorActivity(lifecycleItems, [
      attempt({
        observation_id: "exec:c2",
        snapshot_id: "exec:c2",
        execution_cycle: 2,
        occurred_at: "2026-10-09T19:11:45.000Z",
        filled: false,
        net_edge: "0.009",
      }),
      attempt({
        observation_id: "exec:c1",
        snapshot_id: "exec:c1",
        execution_cycle: 1,
        occurred_at: "2026-10-09T19:11:42.521Z",
      }),
    ]);
    assert.deepEqual(
      merged.map((item) => item.title),
      ["Price-2 accepted", "Price-2 accepted", "Qualifying opportunity"],
    );
    assert.equal(merged[0]?.price2?.filled, false);
    assert.equal(merged[0]?.price2?.executionCycle, 2);
    assert.equal(merged[1]?.price2?.executionCycle, 1);
    assert.match(merged[1]?.detail ?? "", /accepted · not a fill/);
    assert.doesNotMatch(merged[1]?.detail ?? "", /0\.6631|66\.31%/);
    assert.match(merged[1]?.detail ?? "", /net 1\.20%/);
  });

  it("renders missing snapshot and older rows without inventing odds", () => {
    const missing = activityFromPrice2(
      attempt({
        observation_id: "miss-1",
        source: "lifecycle_rejection",
        snapshot_id: null,
        status: "incomplete_unavailable",
        accepted: false,
        filled: false,
        net_edge: null,
        guaranteed_profit: null,
        execution_size: null,
        elapsed_ms: null,
        started_at: null,
        finished_at: null,
        legs: [],
        rejection_reason: "execution_reprice_failed",
      }),
    );
    assert.equal(missing.title, "Price-2 incomplete/unavailable");
    assert.match(missing.detail, /quotes not recorded/);
    assert.equal(missing.price2?.legs.length, 0);
    const old = activityFromPrice2(
      attempt({
        status: "rejected",
        accepted: false,
        filled: false,
        legs: [],
        execution_size: null,
        oldest_quote_age_ms: null,
        skew_ms: null,
        rejection_reason: "execution_reprice_stale",
        net_edge: null,
      }),
    );
    assert.equal(price2Title("rejected"), "Price-2 rejected");
    assert.match(old.detail, /not recorded/);
    assert.match(old.detail, /execution_reprice_stale/);
    assert.doesNotMatch(old.detail, /gross /);
  });

  it("formats local clock with milliseconds after hydration", () => {
    const local = new Date(2026, 9, 9, 20, 11, 42, 521);
    assert.equal(formatDateLocalClockWithMs(local), "20:11:42.521");
    assert.equal(formatLocalClockWithMs("2026-10-09T19:11:42.521Z"), "2026-10-09T19:11:42.521Z");
    const now = Date.parse("2026-10-09T19:11:42.521Z");
    const hydrated = formatLocalClockWithMs("2026-10-09T19:11:42.521Z", now);
    const expected = formatDateLocalClockWithMs(new Date("2026-10-09T19:11:42.521Z"));
    assert.equal(hydrated, expected);
    const line = price2TimingLine(
      {
        startedAt: "2026-10-09T19:11:41.709Z",
        finishedAt: "2026-10-09T19:11:42.521Z",
        elapsedMs: 812,
        occurredAt: "2026-10-09T19:11:45.000Z",
      },
      now,
    );
    assert.match(line, /quote evaluation 812 ms/);
    assert.match(line, /audit /);
    assert.doesNotMatch(line, /order-submit/);
    const unknown = price2TimingLine({ startedAt: null, finishedAt: null, elapsedMs: null }, now);
    assert.match(unknown, /quote evaluation duration not recorded/);
    assert.match(price2CompactDetail(attempt()), /cycle 1/);
    assert.match(
      price2CompactDetail(attempt({ trade_linked: true, filled: false })),
      /trade linked · fill not recorded/,
    );
    const mapped = activityFromPrice2(attempt({ finished_at: null, elapsed_ms: null }));
    assert.equal(mapped.price2?.finishedAt, null);
    assert.equal(mapped.price2?.elapsedMs, null);
  });

  it("bounds the follow-up read to visible opportunity ids", () => {
    const events = [
      lifecycle({
        event_id: "q1",
        event_type: "qualifying_detected",
        occurred_at: "2026-10-09T19:12:00.000Z",
      }),
      lifecycle({
        event_id: "other",
        opportunity_id: "watch:other",
        event_type: "qualifying_lost",
        occurred_at: "2026-10-09T19:11:00.000Z",
      }),
      lifecycle({
        event_id: "noise",
        event_type: "paper_fill_rejected",
        occurred_at: "2026-10-09T19:09:00.000Z",
      }),
    ];
    assert.deepEqual(visibleOpportunityIds(events), [
      "watch:mkt-moreirense",
      "watch:other",
    ]);
    assert.equal(oldestVisibleOccurredAt(events), "2026-10-09T19:11:00.000Z");
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const feed = readFileSync(join(frontendRoot, "components/activity-feed.tsx"), "utf8");
    const vitest = readFileSync(join(frontendRoot, "vitest.config.ts"), "utf8");
    assert.match(vitest, /price2-activity-feed\.test\.ts/);
    assert.match(page, /getPrice2ActivityAttempts/);
    assert.match(page, /price2ActivityQuery/);
    assert.match(page, /mergeOperatorActivity/);
    const query = price2ActivityQuery([], null, Date.parse("2026-10-09T19:45:00.000Z"));
    assert.match(query, /include_recent=true/);
    assert.match(query, /since=/);
    assert.doesNotMatch(query, /opportunity_ids=/);
    assert.match(feed, /Price-2 attempts/);
    assert.match(feed, /RECORDED AUDIT/);
    assert.match(feed, /Price-2 quote detail/);
    assert.doesNotMatch(feed, /available_depth/);
  });
});
