import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  ACTIVE_TRADE_LOG_EMPTY,
  activeTradeEventQuestion,
  activeTradePayloadBits,
  activeTradeTimelineLines,
} from "./active-trade-timeline-display";
import { readFileSync } from "node:fs";
import { join } from "node:path";

describe("ACTIVE TRADE timeline display", () => {
  it("keeps an honest empty journal state", () => {
    assert.deepEqual(activeTradeTimelineLines([]), [ACTIVE_TRADE_LOG_EMPTY]);
  });

  it("renders persisted event type and reason without inventing fills", () => {
    const lines = activeTradeTimelineLines([
      {
        event_id: "refresh-result:t1",
        occurred_at: "2026-09-20T14:00:05.000Z",
        event_type: "active_refresh_result",
        reason_code: "refresh_retry_wait",
        operator_copy: "ACTIVE refresh retry-wait",
        trade_id: "t1",
        cycle_id: "c1",
        venue: "kalshi",
        data_kind: "persisted_active_trade_journal",
      },
    ]);
    assert.match(lines[0], /active_refresh_result/);
    assert.match(lines[0], /refresh_retry_wait/);
    assert.match(lines[0], /kalshi/);
  });

  it("labels operator questions for qualify, fill attempt, management, and finish", () => {
    assert.equal(activeTradeEventQuestion("entry_decision"), "Why did HOT/BACKGROUND qualify this?");
    assert.equal(activeTradeEventQuestion("entry_attempt"), "What happened on the initial fill attempt?");
    assert.equal(activeTradeEventQuestion("entry_fill"), "What was the initial fill result?");
    assert.equal(activeTradeEventQuestion("entry_no_fill"), "What was the initial fill result?");
    assert.equal(activeTradeEventQuestion("promoted_to_active"), "How did ACTIVE TRADE management start?");
    assert.equal(activeTradeEventQuestion("no_action"), "Why didn't we top up?");
    assert.equal(activeTradeEventQuestion("active_refresh_result"), "What happened on this 5s cycle?");
    assert.equal(activeTradeEventQuestion("entry_partial_fill"), "Where did a partial fill occur?");
    assert.equal(activeTradeEventQuestion("entry_recovery_fill"), "What recovery was attempted?");
    assert.equal(activeTradeEventQuestion("settled"), "How did the trade finish?");
    assert.match(
      activeTradePayloadBits({
        pricing_lane: "hot",
        net_edge: "0.012",
        residual_gbp: "1.25",
        native_ids: [{ venue: "kalshi" }],
      }) ?? "",
      /pricing lane hot/,
    );
  });

  it("operator console has a Trade log affordance separate from Why?", () => {
    const scan = readFileSync(join(process.cwd(), "components/run-paper-scan.tsx"), "utf8");
    const log = readFileSync(join(process.cwd(), "components/active-trade-log.tsx"), "utf8");
    const book = readFileSync(join(process.cwd(), "components/paper-trade-book.tsx"), "utf8");
    const detail = readFileSync(join(process.cwd(), "app/paper/[tradeId]/page.tsx"), "utf8");
    const why = readFileSync(join(process.cwd(), "components/venue-health-bar.tsx"), "utf8");
    const whyHelper = readFileSync(join(process.cwd(), "lib/venue-degradation-incident.ts"), "utf8");
    assert.match(scan, /<ActiveTradeLog/);
    assert.match(scan, /active_trade_timeline/);
    assert.doesNotMatch(scan, /downloadVenueWhyIncident|getVenueDegradationIncident/);
    assert.match(log, /getActiveTradeEvents/);
    assert.match(log, /Trade log · ACTIVE TRADE history/);
    assert.match(log, /qualifying HOT\/BACKGROUND decision/);
    assert.match(log, /same-cycle/);
    assert.doesNotMatch(log, /downloadVenueWhyIncident|venue-degradation|Why\?/);
    assert.match(book, /#trade-log/);
    assert.match(book, /<ActiveTradeLog/);
    assert.match(detail, /<ActiveTradeLog/);
    assert.match(why, /downloadVenueWhyIncident/);
    assert.doesNotMatch(whyHelper, /active_trade_context|query_active_trade_events/);
  });
});
