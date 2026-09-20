import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { activeTradeTimelineLines } from "./active-trade-timeline-display";
import { readFileSync } from "node:fs";
import { join } from "node:path";

describe("ACTIVE TRADE timeline display", () => {
  it("keeps an honest empty journal state", () => {
    assert.deepEqual(activeTradeTimelineLines([]), [
      "ACTIVE TRADE journal · no persisted events yet",
    ]);
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

  it("operator console reads the compact timeline from live-refresh", () => {
    const scan = readFileSync(join(process.cwd(), "components/run-paper-scan.tsx"), "utf8");
    assert.match(scan, /active_trade_timeline/);
    assert.match(scan, /activeTradeTimelineLines/);
  });
});
