import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { LiveRefreshStatus } from "./api";
import { dualScanStatusLines, fastScanCopy, fullSweepCopy } from "./scan-status-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function status(overrides: Partial<LiveRefreshStatus> = {}): LiveRefreshStatus {
  return {
    discovery_source: "matchbook",
    matching_venue: "polymarket",
    server_loop_enabled: true,
    interval_seconds: 30,
    cycle_in_progress: false,
    live_scores: "unavailable_unless_matchbook_payload_includes_scores",
    discovered_fixtures: [],
    hot: {
      cadence_seconds: 30,
      cycle_timeout_seconds: 25,
      last_completed_at: "2026-09-14T12:00:00Z",
      last_duration_ms: 4100,
      next_due_at: "2026-09-14T12:00:18Z",
      fixture_count: 7,
      not_evaluated_count: 2,
    },
    universe: {
      cadence_seconds: 180,
      generation_budget_seconds: 150,
      generation_work_used_s: 41,
      chunk_last_duration_ms: 8000,
      fixture_count: 104,
      evaluated_count: 60,
      not_evaluated_count: 44,
    },
    ...overrides,
  };
}

describe("dual cadence operator copy", () => {
  it("renders Fast scan and Full sweep as distinct lines", () => {
    const now = Date.parse("2026-09-14T12:00:12Z");
    const lines = dualScanStatusLines(status(), now);
    assert.equal(lines.length, 2);
    assert.match(lines[0], /Fast scan/);
    assert.match(lines[1], /Full sweep/);
    assert.doesNotMatch(lines.join(" "), /^Last scan /);
    assert.match(fastScanCopy(status(), now).detail, /partial \(2 not evaluated\)/);
    assert.doesNotMatch(fastScanCopy(status(), now).detail, /scan_cycle_timeout/);
    assert.match(fullSweepCopy(status(), now).detail, /104 universe/);
    const persistFailed = status({
      last_error: null,
      hot: {
        cadence_seconds: 30,
        cycle_timeout_seconds: 25,
        last_completed_at: "2026-09-14T12:00:00Z",
        last_duration_ms: 4100,
        next_due_at: "2026-09-14T12:00:18Z",
        fixture_count: 7,
        not_evaluated_count: 2,
        last_error: null,
        persist_ok: false,
        last_persist_error: "audit_write_failed",
      },
    });
    assert.match(fastScanCopy(persistFailed, now).detail, /persist\/auto-capture failed/);
    assert.doesNotMatch(fastScanCopy(persistFailed, now).detail, /scan_cycle_timeout/);
    const universePersistFailed = status({
      last_error: null,
      universe: {
        cadence_seconds: 180,
        generation_budget_seconds: 150,
        generation_work_used_s: 41,
        chunk_last_duration_ms: 8000,
        fixture_count: 104,
        evaluated_count: 60,
        not_evaluated_count: 44,
        last_error: null,
        persist_ok: false,
        last_persist_error: "audit_write_failed",
      },
    });
    assert.match(fullSweepCopy(universePersistFailed, now).detail, /persist\/auto-capture failed/);
    assert.doesNotMatch(fullSweepCopy(universePersistFailed, now).detail, /scan_cycle_timeout/);
  });

  it("names active venues on each cadence line when the backend reports them", () => {
    const now = Date.parse("2026-09-14T12:00:12Z");
    const withVenues = status({
      hot: {
        cadence_seconds: 30,
        last_completed_at: "2026-09-14T12:00:00Z",
        last_duration_ms: 4100,
        next_due_at: "2026-09-14T12:00:18Z",
        fixture_count: 7,
        active_venues: ["matchbook", "kalshi"],
      },
      universe: {
        cadence_seconds: 180,
        generation_budget_seconds: 150,
        generation_work_used_s: 41,
        chunk_last_duration_ms: 8000,
        fixture_count: 104,
        evaluated_count: 60,
        not_evaluated_count: 44,
        active_venues: ["matchbook", "polymarket", "kalshi"],
      },
    });
    assert.match(fastScanCopy(withVenues, now).detail, /MB·K/);
    assert.doesNotMatch(fastScanCopy(withVenues, now).detail, /PM/);
    assert.match(fullSweepCopy(withVenues, now).detail, /MB·PM·K/);
  });

  it("keeps persist-failure copy together with lane venue names", () => {
    const now = Date.parse("2026-09-14T12:00:12Z");
    const combined = status({
      last_error: null,
      hot: {
        cadence_seconds: 30,
        cycle_timeout_seconds: 25,
        last_completed_at: "2026-09-14T12:00:00Z",
        last_duration_ms: 4100,
        next_due_at: "2026-09-14T12:00:18Z",
        fixture_count: 7,
        not_evaluated_count: 2,
        persist_ok: false,
        last_persist_error: "audit_write_failed",
        active_venues: ["matchbook", "kalshi"],
      },
      universe: {
        cadence_seconds: 180,
        generation_budget_seconds: 150,
        generation_work_used_s: 41,
        chunk_last_duration_ms: 8000,
        fixture_count: 104,
        evaluated_count: 60,
        not_evaluated_count: 44,
        persist_ok: false,
        last_persist_error: "audit_write_failed",
        active_venues: ["matchbook", "polymarket"],
      },
    });
    assert.match(fastScanCopy(combined, now).detail, /MB·K/);
    assert.match(fastScanCopy(combined, now).detail, /persist\/auto-capture failed/);
    assert.match(fastScanCopy(combined, now).detail, /partial \(2 not evaluated\)/);
    assert.doesNotMatch(fastScanCopy(combined, now).detail, /scan_cycle_timeout/);
    assert.match(fullSweepCopy(combined, now).detail, /MB·PM/);
    assert.match(fullSweepCopy(combined, now).detail, /persist\/auto-capture failed/);
    assert.doesNotMatch(fullSweepCopy(combined, now).detail, /scan_cycle_timeout/);
  });

  it("health bar and scan note no longer ship a single Last scan line", () => {
    const bar = readFileSync(join(frontendRoot, "components/venue-health-bar.tsx"), "utf8");
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    const layout = readFileSync(join(frontendRoot, "app/layout.tsx"), "utf8");
    assert.match(bar, /dualScanStatusLines/);
    assert.match(scan, /dualScanStatusLines/);
    assert.doesNotMatch(bar, /Last scan \$\{/);
    assert.doesNotMatch(scan, /Last scan \{lastCompletedAt/);
    assert.match(scan, /VenueLaneControls/);
    const chips = readFileSync(join(frontendRoot, "components/venue-lane-controls.tsx"), "utf8");
    assert.match(chips, /config_diagnostic/);
    assert.match(scan, /pollLiveStatus/);
    assert.doesNotMatch(scan, /void collectRef\.current\(\)/);
    assert.match(bar, /AUTO PAPER CAPTURE ON/);
    assert.match(bar, /paper_autofill_enabled/);
    assert.match(scan, /AUTO PAPER CAPTURE ON/);
    assert.match(layout, /PAPER MODE · NO EXECUTION/);
  });

  it("routes primary Run scan to HOT and labels full discovery as advanced", () => {
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    const api = readFileSync(join(frontendRoot, "lib/api.ts"), "utf8");
    assert.match(scan, /await collect\("hot"\)/);
    assert.match(scan, /runPaperHotRefresh\(payload\)/);
    assert.match(scan, /Run scan/);
    assert.match(scan, /Run full diagnostic/);
    assert.match(scan, /collect\("diagnostic"\)/);
    assert.match(scan, /does not rediscover the full universe/);
    assert.match(api, /\/paper\/collect\/hot/);
    assert.match(api, /PAPER_HOT_REFRESH_TIMEOUT_MS = 35_000/);
    assert.match(api, /\/paper\/collect`/);
  });
});
