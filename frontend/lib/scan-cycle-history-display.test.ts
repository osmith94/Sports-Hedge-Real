import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { PaperScanCycleRecord } from "./api";
import {
  SCAN_CYCLE_EMPTY,
  SCAN_CYCLE_TITLE,
  SCAN_CYCLE_UNAVAILABLE,
  scanCycleBadgeLabel,
  scanCycleHealthLabel,
  scanCycleLaneLabel,
  scanCycleRow,
  scanCycleRows,
} from "./scan-cycle-history-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function cycle(overrides: Partial<PaperScanCycleRecord> = {}): PaperScanCycleRecord {
  return {
    cycle_id: "hot:2026-09-16T18:00:00+00:00:2026-09-16T18:00:02+00:00",
    started_at: "2026-09-16T18:00:00Z",
    completed_at: "2026-09-16T18:00:02Z",
    scan_lane: "hot",
    duration_ms: 2000,
    fixture_count: 3,
    evaluated_count: 2,
    not_evaluated_count: 1,
    matched_event_pairs: 1,
    matched_market_pairs: 4,
    paper_decision_count: 0,
    qualifying_arb_count: 0,
    venue_health: { matchbook: "ok", polymarket: "ok", kalshi: "ok" },
    degraded: false,
    ...overrides,
  };
}

describe("scan cycle history presentation", () => {
  it("renders a zero-decision completed cycle without synthesizing market rows", () => {
    const row = scanCycleRow(cycle({ paper_decision_count: 0, qualifying_arb_count: 0 }));
    assert.equal(row.laneLabel, "HOT");
    assert.equal(row.paperDecisionLabel, "0");
    assert.equal(row.qualifyingLabel, "0");
    assert.equal(row.healthLabel, "venues ok");
  });

  it("keeps HOT and UNIVERSE cycle rows distinct", () => {
    const rows = scanCycleRows([
      cycle({ cycle_id: "universe-1", scan_lane: "universe", completed_at: "2026-09-16T18:01:00Z" }),
      cycle({ cycle_id: "hot-1", scan_lane: "hot" }),
    ]);
    assert.deepEqual(rows.map((item) => item.laneLabel), ["UNIVERSE", "HOT"]);
    assert.equal(scanCycleLaneLabel("hot"), "HOT");
  });

  it("labels provider unavailability as health, not scanner failure", () => {
    const row = scanCycleRow(
      cycle({
        degraded: true,
        last_error: null,
        venue_health: { matchbook: "ok", polymarket: "ok", kalshi: "unavailable" },
      }),
    );
    assert.match(row.healthLabel, /kalshi unavailable/);
    assert.doesNotMatch(row.healthLabel, /scan failed/i);
    assert.equal(scanCycleHealthLabel(cycle({ last_error: "scan_cycle_timeout after 25s" })), "scanner error · scan_cycle_timeout after 25s");
  });

  it("keeps empty and unavailable states honest", () => {
    assert.equal(scanCycleBadgeLabel(true, []), "EMPTY");
    assert.equal(scanCycleBadgeLabel(false, null), "UNAVAILABLE");
    assert.match(SCAN_CYCLE_EMPTY, /Zero-decision cycles still appear/);
    assert.match(SCAN_CYCLE_UNAVAILABLE, /No fabricated cycles/);
  });
});

describe("scan cycle history console wiring", () => {
  it("loads latest 100 through live-refresh poll / router refresh without mixing market audit rows", () => {
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const panel = readFileSync(join(frontendRoot, "components/scan-cycle-history-panel.tsx"), "utf8");
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    const api = readFileSync(join(frontendRoot, "lib/api.ts"), "utf8");
    const hotIndex = page.indexOf("<HotFixturesPanel");
    const monitorIndex = page.indexOf("<OpportunityMonitor");
    const cycleIndex = page.indexOf("<ScanCycleHistoryPanel");
    const auditIndex = page.indexOf("<PaperScanHistoryTable");
    assert.ok(hotIndex > 0);
    assert.ok(monitorIndex > hotIndex);
    assert.ok(cycleIndex > monitorIndex);
    assert.ok(auditIndex > cycleIndex);
    assert.match(page, /getPaperScanCycles\("limit=100"\)/);
    assert.match(page, /recent_scan_cycles/);
    assert.match(api, /\/paper\/scan-cycles/);
    assert.match(scan, /router\.refresh\(\)/);
    assert.match(scan, /last_completed_at/);
    assert.equal(SCAN_CYCLE_TITLE, "Scan cycle history");
    assert.match(panel, /scanCycleRows/);
    assert.doesNotMatch(panel, /getPaperScans/);
    assert.doesNotMatch(panel, /PaperScanRecord/);
    assert.match(page, /market-decision audit/);
  });
});
