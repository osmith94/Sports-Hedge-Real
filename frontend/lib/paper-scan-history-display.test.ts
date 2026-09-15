import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { PaperScanRecord } from "./api";
import {
  AUDIT_AGE_TICK_MS,
  DEFAULT_PAPER_SCAN_HISTORY_SORT,
  ariaSortForPaperScanColumn,
  auditScanTimestampTitle,
  formatAuditScanAge,
  initialPaperScanHistoryDirection,
  nextPaperScanHistorySort,
  paperScanHistoryStatusText,
  paperScanSortIndicator,
  parseAuditScannedAtMs,
  sortPaperScanHistory,
  startSharedAuditAgeTimer,
} from "./paper-scan-history-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function scan(overrides: Partial<PaperScanRecord> & Pick<PaperScanRecord, "record_id">): PaperScanRecord {
  const { record_id, ...rest } = overrides;
  return {
    record_id,
    scanned_at: "2026-09-15T12:00:00.000Z",
    canonical_event_id: "evt-1",
    canonical_market_id: "mkt-1",
    competition: "Premier League",
    home_team: "Leeds United",
    away_team: "Newcastle United",
    kickoff_utc: "2026-09-20T15:00:00.000Z",
    market_family: "match_result",
    period: "full_time",
    venues: ["matchbook", "polymarket"],
    source_market_ids: ["mb-1", "pm-1"],
    mapping_confidence: 0.9,
    is_arbitrage: false,
    eligible_for_paper_simulation: false,
    gross_edge: 0.01,
    net_edge: 0.008,
    executable_stake_gbp: 100,
    guaranteed_profit_gbp: 0.8,
    execution_risk_score: 0.2,
    execution_risk_band: "low",
    rejection_reasons: [],
    ...rest,
  };
}

describe("paper scan history surface", () => {
  it("loads the latest 100 audit observations and labels that window", () => {
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const api = readFileSync(join(frontendRoot, "lib/api.ts"), "utf8");
    assert.match(page, /getPaperScans\("limit=100"\)/);
    assert.match(api, /query = "limit=100"/);
    assert.match(page, /Latest 100 audit observations/);
    assert.match(page, /Not current scanner radar/);
    assert.match(page, /LATEST 100 AUDIT/);
    assert.match(page, /Sorting applies to this loaded/);
    assert.match(page, /Age uses each row/);
    assert.match(page, /<PaperScanHistoryTable scans=\{scans\}/);
    assert.doesNotMatch(page, /scans\.filter/);
    assert.doesNotMatch(page, /current_radar_rows/);
    assert.doesNotMatch(page, /SCANNER DATA/);
  });
});

describe("paper scan history age", () => {
  const scannedAt = "2026-09-15T12:00:00.000Z";
  const origin = Date.parse(scannedAt);

  it("formats 0s..59s then minutes, hours and days from scanned_at", () => {
    assert.equal(formatAuditScanAge(scannedAt, origin), "0s");
    assert.equal(formatAuditScanAge(scannedAt, origin + 1_000), "1s");
    assert.equal(formatAuditScanAge(scannedAt, origin + 59_000), "59s");
    assert.equal(formatAuditScanAge(scannedAt, origin + 60_000), "1m");
    assert.equal(formatAuditScanAge(scannedAt, origin + 61_000), "1m");
    assert.equal(formatAuditScanAge(scannedAt, origin + 119_999), "1m");
    assert.equal(formatAuditScanAge(scannedAt, origin + 120_000), "2m");
    assert.equal(formatAuditScanAge(scannedAt, origin + 3_599_000), "59m");
    assert.equal(formatAuditScanAge(scannedAt, origin + 3_600_000), "1h");
    assert.equal(formatAuditScanAge(scannedAt, origin + 86_399_000), "23h");
    assert.equal(formatAuditScanAge(scannedAt, origin + 86_400_000), "1d");
    assert.equal(formatAuditScanAge(scannedAt, origin + 172_800_000), "2d");
  });

  it("crosses the 59s→1m boundary at exactly 60 seconds", () => {
    assert.equal(formatAuditScanAge(scannedAt, origin + 59_999), "59s");
    assert.equal(formatAuditScanAge(scannedAt, origin + 60_000), "1m");
  });

  it("ages from the audit scanned_at, never treating browser receipt as the observation", () => {
    const receipt = Date.parse("2026-09-15T12:05:00.000Z");
    assert.equal(formatAuditScanAge(scannedAt, receipt), "5m");
    assert.equal(parseAuditScannedAtMs(scannedAt), origin);
    assert.notEqual(parseAuditScannedAtMs(scannedAt), receipt);
    assert.equal(formatAuditScanAge("2026-09-15T12:05:00.000Z", receipt), "0s");
  });

  it("exposes the exact scanned_at string as the tooltip title", () => {
    assert.equal(auditScanTimestampTitle(scannedAt), scannedAt);
    assert.equal(auditScanTimestampTitle(""), "scanned_at unavailable");
    assert.equal(formatAuditScanAge("not-a-timestamp", origin), "—");
    assert.equal(formatAuditScanAge(scannedAt, origin - 5_000), "0s");
  });
});

describe("paper scan history shared age timer", () => {
  it("starts one shared 1s timer and ticks the supplied now clock", () => {
    const ticks: number[] = [];
    const handlers: Array<() => void> = [];
    let cleared = 0;
    const stop = startSharedAuditAgeTimer((nowMs) => ticks.push(nowMs), {
      now: () => 1_700_000_000_000,
      setInterval: (handler) => {
        handlers.push(handler);
        return 7 as unknown as ReturnType<typeof setInterval>;
      },
      clearInterval: () => {
        cleared += 1;
      },
      tickMs: AUDIT_AGE_TICK_MS,
    });
    assert.equal(handlers.length, 1);
    assert.equal(AUDIT_AGE_TICK_MS, 1000);
    handlers[0]();
    handlers[0]();
    assert.deepEqual(ticks, [1_700_000_000_000, 1_700_000_000_000]);
    stop();
    assert.equal(cleared, 1);
  });
});

describe("paper scan history sort", () => {
  it("defaults to newest scanned_at first", () => {
    const older = scan({ record_id: "older", scanned_at: "2026-09-15T12:00:00.000Z" });
    const newer = scan({ record_id: "newer", scanned_at: "2026-09-15T12:00:25.000Z" });
    const sorted = sortPaperScanHistory([older, newer], DEFAULT_PAPER_SCAN_HISTORY_SORT);
    assert.deepEqual(
      sorted.map((row) => row.record_id),
      ["newer", "older"],
    );
    const preserved = sortPaperScanHistory([older, newer], null);
    assert.deepEqual(
      preserved.map((row) => row.record_id),
      ["older", "newer"],
    );
  });

  it("uses sensible first directions and toggles the active header", () => {
    assert.equal(initialPaperScanHistoryDirection("age"), "desc");
    assert.equal(initialPaperScanHistoryDirection("netEdge"), "desc");
    assert.equal(initialPaperScanHistoryDirection("event"), "asc");
    const first = nextPaperScanHistorySort(null, "netEdge");
    assert.deepEqual(first, { column: "netEdge", direction: "desc" });
    const toggled = nextPaperScanHistorySort(first, "netEdge");
    assert.deepEqual(toggled, { column: "netEdge", direction: "asc" });
    const switched = nextPaperScanHistorySort(toggled, "event");
    assert.deepEqual(switched, { column: "event", direction: "asc" });
  });

  it("exposes aria-sort and a visible indicator on the active column only", () => {
    const sort = { column: "age" as const, direction: "desc" as const };
    assert.equal(ariaSortForPaperScanColumn("age", sort), "descending");
    assert.equal(ariaSortForPaperScanColumn("event", sort), "none");
    assert.equal(paperScanSortIndicator("age", sort), "▼");
    assert.equal(paperScanSortIndicator("event", sort), "");
  });

  it("sorts text columns alphabetically when toggled", () => {
    const leeds = scan({ record_id: "leeds", home_team: "Leeds United", away_team: "Newcastle United" });
    const arsenal = scan({ record_id: "arsenal", home_team: "Arsenal", away_team: "Chelsea" });
    const asc = sortPaperScanHistory([leeds, arsenal], { column: "event", direction: "asc" });
    const desc = sortPaperScanHistory([leeds, arsenal], { column: "event", direction: "desc" });
    assert.deepEqual(
      asc.map((row) => row.record_id),
      ["arsenal", "leeds"],
    );
    assert.deepEqual(
      desc.map((row) => row.record_id),
      ["leeds", "arsenal"],
    );
  });

  it("sorts numeric columns numerically, with unknown/null last in both directions", () => {
    const rows = [
      scan({ record_id: "null-net", net_edge: null, guaranteed_profit_gbp: null }),
      scan({ record_id: "nan-net", net_edge: "not-a-number", guaranteed_profit_gbp: "nope" }),
      scan({ record_id: "low", net_edge: "0.002", guaranteed_profit_gbp: 1 }),
      scan({ record_id: "high", net_edge: 0.02, guaranteed_profit_gbp: 8 }),
    ];
    const desc = sortPaperScanHistory(rows, { column: "netEdge", direction: "desc" });
    const asc = sortPaperScanHistory(rows, { column: "netEdge", direction: "asc" });
    assert.deepEqual(
      desc.map((row) => row.record_id),
      ["high", "low", "null-net", "nan-net"],
    );
    assert.deepEqual(
      asc.map((row) => row.record_id),
      ["low", "high", "null-net", "nan-net"],
    );
  });

  it("keeps rejected current audit rows visible instead of hiding them", () => {
    const rejected = scan({
      record_id: "rejected",
      eligible_for_paper_simulation: false,
      is_arbitrage: false,
      rejection_reasons: ["insufficient_depth", "stale_quote"],
    });
    const eligible = scan({
      record_id: "eligible",
      eligible_for_paper_simulation: true,
      is_arbitrage: true,
    });
    const sorted = sortPaperScanHistory([rejected, eligible], { column: "status", direction: "asc" });
    assert.equal(sorted.length, 2);
    assert.ok(sorted.some((row) => row.record_id === "rejected"));
    assert.match(paperScanHistoryStatusText(rejected), /insufficient depth/);
    assert.equal(paperScanHistoryStatusText(eligible), "Paper eligible");
  });
});

describe("paper scan history table contract", () => {
  const table = readFileSync(join(frontendRoot, "components/paper-scan-history-table.tsx"), "utf8");
  const display = readFileSync(join(frontendRoot, "lib/paper-scan-history-display.ts"), "utf8");

  it("uses one shared timer and the audit scanned_at for age labels", () => {
    assert.match(table, /startSharedAuditAgeTimer\(setNowMs/);
    assert.match(table, /formatAuditScanAge\(item\.scanned_at, nowMs\)/);
    assert.match(table, /title=\{auditScanTimestampTitle\(item\.scanned_at\)\}/);
    assert.match(table, /dateTime=\{item\.scanned_at\}/);
    assert.equal([...table.matchAll(/setInterval/g)].length, 0);
    assert.equal([...table.matchAll(/startSharedAuditAgeTimer\(/g)].length, 1);
    assert.match(display, /schedule\(\(\) => onTick\(now\(\)\)/);
    assert.doesNotMatch(table, /scans\.map\([\s\S]*setInterval/);
  });

  it("uses keyboard-accessible sortable headers with a visible indicator", () => {
    assert.match(table, /type="button"/);
    assert.match(table, /aria-sort=\{ariaSort\}/);
    assert.match(table, /aria-label=\{`Sort by \$\{label\}/);
    assert.match(table, /className="sort-indicator"/);
    assert.match(table, /Scanned \/ Age|"age"/);
    assert.match(table, /scope="col"/);
  });

  it("does not filter, replace, or current-state-dedupe the loaded audit window", () => {
    assert.doesNotMatch(table, /\.filter\(/);
    assert.doesNotMatch(table, /current_radar_rows/);
    assert.doesNotMatch(table, /latestByMarket/);
    assert.doesNotMatch(table, /DELETE/);
    assert.match(table, /sortPaperScanHistory\(scans, sort\)/);
    assert.match(table, /Historical audit window/);
  });
});
