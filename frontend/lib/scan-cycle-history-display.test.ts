import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { PaperScanCycleRecord } from "./api";
import {
  SCAN_CYCLE_COPY,
  SCAN_CYCLE_EMPTY,
  SCAN_CYCLE_HEADERS,
  SCAN_CYCLE_TITLE,
  SCAN_CYCLE_UNAVAILABLE,
  scanCycleBadgeLabel,
  scanCycleCoverageLabel,
  scanCycleDiagnosticLines,
  scanCycleHealthLabel,
  scanCycleLaneLabel,
  scanCycleLatestSummary,
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
    last_error: null,
    ...overrides,
  };
}

describe("scan cycle history presentation", () => {
  it("renders a zero-decision completed cycle without synthesizing market rows", () => {
    const row = scanCycleRow(cycle({ paper_decision_count: 0, qualifying_arb_count: 0, not_evaluated_count: 0 }));
    assert.equal(row.laneLabel, "HOT pricing");
    assert.equal(row.fixtureLabel, "3 catalogue rows");
    assert.equal(row.paperDecisionLabel, "0");
    assert.equal(row.qualifyingLabel, "0");
    assert.equal(row.healthLabel, "venues ok");
  });

  it("keeps HOT and UNIVERSE cycle rows distinct", () => {
    const rows = scanCycleRows([
      cycle({ cycle_id: "universe-1", scan_lane: "universe", completed_at: "2026-09-16T18:01:00Z", fixture_count: 104 }),
      cycle({ cycle_id: "hot-1", scan_lane: "hot" }),
    ]);
    assert.deepEqual(rows.map((item) => item.laneLabel), ["UNIVERSE discovery", "HOT pricing"]);
    assert.equal(rows[0].fixtureLabel, "104 fixtures");
    assert.equal(rows[1].fixtureLabel, "3 catalogue rows");
    assert.equal(scanCycleLaneLabel("hot"), "HOT pricing");
    assert.equal(scanCycleLaneLabel("background"), "BACKGROUND pricing");
    assert.equal(scanCycleLaneLabel("universe"), "UNIVERSE discovery");
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
    assert.equal(
      scanCycleHealthLabel(cycle({ last_error: "scan_cycle_timeout after 25s" })),
      "provider failure · scan_cycle_timeout after 25s",
    );
    assert.equal(
      scanCycleHealthLabel(cycle({ last_error: "list_events_timeout after 15s" })),
      "provider failure · list_events_timeout after 15s",
    );
  });

  it("does not promote unsupported Matchbook market skips or deadline leftovers to scanner error", () => {
    assert.equal(
      scanCycleHealthLabel(
        cycle({
          last_error: null,
          not_evaluated_count: 12,
          degraded: true,
          operator_summary: "UNIVERSE discovery · partial · 12 not evaluated",
        }),
      ),
      "partial · 12 not evaluated",
    );
    assert.equal(
      scanCycleHealthLabel(
        cycle({
          last_error: null,
          not_evaluated_count: 0,
          matched_event_pairs: 37,
          matched_market_pairs: 0,
          operator_summary: "2 unsupported markets skipped",
        }),
      ),
      "unsupported markets skipped",
    );
    assert.equal(
      scanCycleHealthLabel(
        cycle({
          last_error: null,
          not_evaluated_count: 0,
          matched_event_pairs: 37,
          matched_market_pairs: 0,
          operator_summary: "UNIVERSE discovery",
        }),
      ),
      "evaluated · 0 equivalent markets",
    );
  });

  it("keeps every loaded cycle in one row set rather than a second recent slice", () => {
    const cycles = Array.from({ length: 12 }, (_, index) =>
      cycle({ cycle_id: `cycle-${index}`, scan_lane: index % 2 === 0 ? "hot" : "universe" }),
    );
    const rows = scanCycleRows(cycles);
    assert.equal(rows.length, 12);
    assert.deepEqual(rows.map((item) => item.id), cycles.map((item) => item.cycle_id));
  });

  it("keeps empty and unavailable states honest", () => {
    assert.equal(scanCycleBadgeLabel(true, []), "EMPTY");
    assert.equal(scanCycleBadgeLabel(false, null), "UNAVAILABLE");
    assert.equal(scanCycleLatestSummary(false, null), "unavailable");
    assert.equal(scanCycleLatestSummary(true, []), "no completed cycles");
    assert.equal(
      scanCycleLatestSummary(true, [cycle({ not_evaluated_count: 0 })]),
      "latest HOT pricing · venues ok",
    );
    assert.equal(scanCycleCoverageLabel(cycle({ scan_lane: "background", fixture_count: 491 })), "491 catalogue rows");
    assert.equal(scanCycleCoverageLabel(cycle({ scan_lane: "hot", fixture_count: 1 })), "1 catalogue row");
    assert.equal(
      scanCycleCoverageLabel(cycle({
        scan_lane: "hot",
        fixture_count: 2,
        operator_summary: "11 HOT fixtures · 70 rows · 8 fixtures touched · 2 evaluated · 43 rows deadline/capacity missed",
      })),
      "11 HOT fixtures · 70 rows · 8 fixtures touched · 2 evaluated · 43 rows deadline/capacity missed",
    );
    assert.equal(scanCycleCoverageLabel(cycle({ scan_lane: "universe", fixture_count: 1 })), "1 fixture");
    assert.equal(SCAN_CYCLE_HEADERS[3], "Coverage");
    assert.match(SCAN_CYCLE_COPY, /catalogue\/market rows/);
    assert.match(SCAN_CYCLE_EMPTY, /Zero-decision cycles still appear/);
    assert.match(SCAN_CYCLE_UNAVAILABLE, /No fabricated cycles/);
  });
});

describe("scan cycle history console wiring", () => {
  it("loads latest 50 from the scan-cycle endpoint without mixing market audit rows", () => {
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const panel = readFileSync(join(frontendRoot, "components/scan-cycle-history-panel.tsx"), "utf8");
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    const api = readFileSync(join(frontendRoot, "lib/api.ts"), "utf8");
    const provider = readFileSync(join(frontendRoot, "components/live-status-provider.tsx"), "utf8");
    const hotIndex = page.indexOf("<HotFixturesPanel");
    const monitorIndex = page.indexOf("<OpportunityMonitor");
    const cycleIndex = page.indexOf("<ScanCycleHistoryPanel");
    assert.ok(hotIndex > 0);
    assert.ok(monitorIndex > hotIndex);
    assert.ok(cycleIndex > monitorIndex);
    assert.equal(page.indexOf("<PaperScanHistoryTable"), -1);
    assert.doesNotMatch(page, /getPaperScanCycles/);
    assert.doesNotMatch(page, /recent_scan_cycles/);
    assert.match(provider, /SCAN_CYCLE_HISTORY_LIMIT = 50/);
    assert.match(provider, /getPaperScanCycles\(`limit=\$\{SCAN_CYCLE_HISTORY_LIMIT\}`\)/);
    assert.match(api, /query = "limit=50"/);
    assert.match(api, /\/paper\/scan-cycles/);
    assert.match(scan, /router\.refresh\(\)/);
    assert.doesNotMatch(scan, /recent_scan_cycles/);
    assert.equal(SCAN_CYCLE_TITLE, "Scan cycle history");
    assert.equal((panel.match(/<CycleTable/g) || []).length, 1);
    assert.equal((panel.match(/<table/g) || []).length, 1);
    assert.match(panel, /scanCycleRows/);
    assert.doesNotMatch(panel, /recentScanCycleRows/);
    assert.doesNotMatch(panel, /SCAN_CYCLE_RECENT_TITLE/);
    assert.doesNotMatch(panel, /SCAN_CYCLE_DIAGNOSTICS_TITLE/);
    assert.match(panel, /scanCycleLatestSummary/);
    assert.match(panel, /loaded cycle/);
    assert.match(panel, /<details className="discovery-disclosure scan-cycle-history-disclosure">/);
    assert.match(panel, /Show history/);
    assert.match(panel, /Hide history/);
    assert.doesNotMatch(panel, /\sopen[={]/);
    assert.doesNotMatch(panel, /open>/);
    assert.doesNotMatch(panel, /getPaperScans/);
    assert.doesNotMatch(panel, /PaperScanRecord/);
    assert.doesNotMatch(page, /market-decision audit/);
    assert.match(panel, /SCAN_CYCLE_REPORT_ACTION/);
    assert.match(api, /\/paper\/scan-cycle-report/);
  });

  it("formats terminal splits without treating the diagnostic as a live book", () => {
    const lines = scanCycleDiagnosticLines({
      note: "Cycle diagnostic counters from this completed scan. Not live quotes.",
      lane: "background",
      wall_ms: 35400,
      due: 491,
      considered: 491,
      evaluations_per_second: 0.11,
      terminals: {
        evaluated: 4,
        skipped: 0,
        revalidation: 0,
        failed: 0,
        retry_wait: 3,
        deferred: 12,
        not_started: 472,
      },
      leftover_collapsed: 487,
      decisions: 4,
      qualifying: 0,
      promoted_hot: 0,
      provider_io_ms_sum: 32000,
      slot_wait_ms_sum: 4000,
      local_evaluate_ms_sum: 40,
      saved_provider_calls: 0,
      coalesced_provider_calls: 0,
      repeated_exact_id_calls: 2,
      distinct_exact_ids: 10,
      call_shape: {
        sequential_within_item: false,
        pricing_call_shape: "provider_centric_staged_exact_id",
        worker_limit: 8,
        explicit_slice_wall: false,
        provider_limits: { matchbook: 4, kalshi: 4 },
        provider_calls: 7,
      },
      stages: [
        {
          venue: "kalshi",
          stage: "order_book",
          count: 4,
          avg_ms: 8000,
          p50_ms: 8000,
          p95_ms: 8000,
          max_ms: 8000,
          success: 1,
          timeout: 3,
          rate_limit: 0,
          capacity_deferred: 0,
        },
      ],
    });
    const text = lines.join("\n");
    assert.match(text, /Not live quotes/);
    assert.match(text, /not started 472/);
    assert.match(text, /Retry wait 3/);
    assert.match(text, /capacity deferred 12/);
    assert.match(text, /explicit slice wall no/);
    assert.match(text, /sequential within item no/);
    assert.match(text, /kalshi order_book/);
    assert.doesNotMatch(text, /yes_dollars|orderbook_fp|"runners"/);
  });
});
