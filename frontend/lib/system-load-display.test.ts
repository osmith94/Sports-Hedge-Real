import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { LiveRefreshStatus, SystemLoadSummary } from "./api";
import { systemLoadLines } from "./system-load-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function load(overrides: SystemLoadSummary = {}): SystemLoadSummary {
  return {
    hot: {
      fixtures: 8,
      working_set: 18,
      due: 4,
      in_flight: 1,
      retry_wait: 2,
      deferred: 0,
      last_cycle_ms: 3800,
      cadence_seconds: 30,
      cadence_utilisation: 0.1267,
    },
    matchbook: { inflight: 2, limit: 4, waiting: 0 },
    kalshi: { inflight: 1, limit: 4, waiting: 0 },
    universe: {
      evaluated: 24,
      total: 30,
      remaining: 6,
      generation_work_used_s: 42,
      generation_budget_seconds: 150,
      cadence_seconds: 600,
    },
    background: {
      cadence_seconds: 90,
      working_set: 31,
      due: 8,
    },
    catalogue_items: 49,
    ...overrides,
  };
}

function status(overrides: Partial<LiveRefreshStatus> = {}): LiveRefreshStatus {
  return {
    discovery_source: "matchbook",
    matching_venue: "polymarket",
    server_loop_enabled: true,
    interval_seconds: 30,
    cycle_in_progress: false,
    live_scores: "unavailable_unless_matchbook_payload_includes_scores",
    discovered_fixtures: [],
    ...overrides,
  };
}

describe("system load display", () => {
  it("formats compact copy from backend system_load only", () => {
    const lines = systemLoadLines(load());
    assert.deepEqual(
      lines.map((line) => `${line.key}  ${line.detail}`),
      [
        "HOT  8 fixtures · 18 items · 4 due · cycle 3.8s / 30s (13%)",
        "BG  31 items · 8 due · 90s cadence",
        "MB  2/4 in use · queue 0",
        "K  1/4 in use · queue 0",
        "UNI  24/30 evaluated · 42s / 150s · 600s cadence",
        "ALL  49 catalogue items",
      ],
    );
  });

  it("keeps fixture count distinct from catalogue items using backend fields", () => {
    const lines = systemLoadLines(
      load({
        hot: { fixtures: 1, working_set: 3, due: 3, last_cycle_ms: 1200, cadence_seconds: 30 },
        catalogue_items: 3,
      }),
    );
    assert.match(lines[0].detail, /1 fixtures · 3 items/);
    assert.equal(lines[5].detail, "3 catalogue items");
  });

  it("displays backend missing cycle duration without computing utilisation", () => {
    const missing = systemLoadLines(
      load({
        hot: {
          fixtures: 8,
          working_set: 18,
          due: 4,
          last_cycle_ms: null,
          cadence_seconds: 30,
          cadence_utilisation: null,
        },
      }),
    );
    assert.match(missing[0].detail, /cycle — \/ 30s/);
    assert.doesNotMatch(missing[0].detail, /NaN/);
    assert.doesNotMatch(missing[0].detail, /%\)/);
  });

  it("does not reconstruct load from raw lane or provider fields when system_load is absent", () => {
    const absent = systemLoadLines(status({
      hot: { cadence_seconds: 30, fixture_count: 8, last_duration_ms: 3800 },
      price_engine: { hot: { working_set: 18, due: 4 }, background: { working_set: 31 } },
      provider_access: { inflight: { matchbook: 2 }, limits: { matchbook: 4 } },
    }).system_load);
    assert.deepEqual(absent, [{ key: "—", detail: "unavailable" }]);
    assert.doesNotMatch(absent.map((line) => line.detail).join(" "), /8 fixtures/);
    const text = systemLoadLines(load())
      .map((line) => `${line.key} ${line.detail}`)
      .join(" ");
    assert.doesNotMatch(text, /safe/i);
    assert.doesNotMatch(text, /unsafe/i);
    assert.doesNotMatch(text, /p50/i);
    assert.doesNotMatch(text, /p95/i);
  });

  it("renders one header card from existing live-refresh polls without extra work or duplicate copies", () => {
    const display = readFileSync(join(frontendRoot, "lib/system-load-display.ts"), "utf8");
    const card = readFileSync(join(frontendRoot, "components/system-load-summary.tsx"), "utf8");
    const bar = readFileSync(join(frontendRoot, "components/venue-health-bar.tsx"), "utf8");
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    assert.match(bar, /SystemLoadSummaryCard/);
    assert.match(card, /status\?\.system_load/);
    assert.doesNotMatch(scan, /SystemLoadSummaryCard/);
    assert.doesNotMatch(display, /deriveSystemLoad/);
    assert.doesNotMatch(display, /resolveSystemLoad/);
    assert.doesNotMatch(display, /cadenceUtilisation/);
    assert.doesNotMatch(display, /DEFAULT_PROVIDER/);
    assert.doesNotMatch(display, /LiveRefreshStatus/);
    assert.doesNotMatch(display, /price_engine/);
    assert.doesNotMatch(display, /provider_access/);
    assert.doesNotMatch(display, /canonical_evaluated/);
    assert.doesNotMatch(display, /getLiveRefreshStatus/);
    assert.doesNotMatch(display, /fetch\(/);
    assert.doesNotMatch(display, /setInterval/);
    assert.doesNotMatch(card, /getLiveRefreshStatus/);
    assert.doesNotMatch(card, /fetch\(/);
    assert.doesNotMatch(card, /setInterval/);
    assert.doesNotMatch(card, /runPaperCollection/);
    assert.doesNotMatch(card, /collect_and_scan/);
    assert.equal([...bar.matchAll(/setInterval/g)].length, 1);
    assert.match(bar, /applyLatestLiveRefresh/);
    assert.match(scan, /applyLatestLiveRefresh/);
  });
});
