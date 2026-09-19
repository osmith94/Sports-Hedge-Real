import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { LiveRefreshStatus } from "./api";
import {
  cadenceUtilisation,
  deriveSystemLoad,
  resolveSystemLoad,
  systemLoadLines,
} from "./system-load-display";

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
      fixture_count: 8,
      last_duration_ms: 3800,
    },
    universe: {
      cadence_seconds: 180,
      generation_budget_seconds: 150,
      generation_work_used_s: 42,
      canonical_evaluated: 24,
      canonical_work_total: 30,
      canonical_remaining: 6,
      fixture_count: 12,
    },
    price_engine: {
      hot: { working_set: 18, due: 4, in_flight: 1, retry_wait: 2, deferred: 0 },
      background: { working_set: 31 },
    },
    provider_access: {
      inflight: { matchbook: 2, kalshi: 1 },
      waiting: { matchbook: 0, kalshi: 0 },
      limits: { matchbook: 4, kalshi: 4 },
    },
    ...overrides,
  };
}

describe("system load display", () => {
  it("renders the compact current-load card from public status fields", () => {
    const lines = systemLoadLines(status());
    assert.deepEqual(
      lines.map((line) => `${line.key}  ${line.detail}`),
      [
        "HOT  8 fixtures · 18 items · 4 due · cycle 3.8s / 30s (13%)",
        "MB  2/4 in use · queue 0",
        "K  1/4 in use · queue 0",
        "UNI  24/30 evaluated · 42s / 150s",
        "ALL  49 catalogue items",
      ],
    );
    const derived = deriveSystemLoad(status());
    assert.equal(derived.hot?.fixtures, 8);
    assert.equal(derived.hot?.working_set, 18);
    assert.equal(derived.catalogue_items, 49);
    assert.notEqual(derived.catalogue_items, derived.hot?.fixtures);
  });

  it("counts TOTAL 2.5/3.5/4.5 as extra catalogue items, not extra fixtures", () => {
    const load = deriveSystemLoad(
      status({
        hot: { cadence_seconds: 30, fixture_count: 1, last_duration_ms: 1200 },
        price_engine: {
          hot: { working_set: 3, due: 3 },
          background: { working_set: 0 },
        },
      }),
    );
    assert.equal(load.hot?.fixtures, 1);
    assert.equal(load.hot?.working_set, 3);
    assert.equal(load.catalogue_items, 3);
  });

  it("handles missing and zero HOT cycle duration without NaN utilisation", () => {
    assert.equal(cadenceUtilisation(null, 30), null);
    assert.equal(cadenceUtilisation(3800, 0), null);
    assert.equal(cadenceUtilisation(0, 30), 0);
    assert.equal(cadenceUtilisation(Number.NaN, 30), null);
    const missing = systemLoadLines(
      status({
        hot: { cadence_seconds: 30, fixture_count: 8, last_duration_ms: null },
        system_load: undefined,
        price_engine: { hot: { working_set: 18, due: 4 }, background: { working_set: 31 } },
      }),
    );
    assert.match(missing[0].detail, /cycle — \/ 30s/);
    assert.doesNotMatch(missing[0].detail, /NaN/);
    assert.doesNotMatch(missing[0].detail, /%\)/);
  });

  it("prefers backend system_load when present and does not invent a safe/unsafe score", () => {
    const live = status({
      system_load: {
        hot: {
          fixtures: 2,
          working_set: 5,
          due: 1,
          last_cycle_ms: 1500,
          cadence_seconds: 30,
          cadence_utilisation: 0.05,
        },
        matchbook: { inflight: 0, limit: 4, waiting: 0 },
        kalshi: { inflight: 0, limit: 4, waiting: 0 },
        universe: {
          evaluated: 3,
          total: 10,
          remaining: 7,
          generation_work_used_s: 12,
          generation_budget_seconds: 150,
        },
        catalogue_items: 9,
      },
    });
    const resolved = resolveSystemLoad(live);
    assert.equal(resolved.catalogue_items, 9);
    const text = systemLoadLines(live)
      .map((line) => `${line.key} ${line.detail}`)
      .join(" ");
    assert.match(text, /HOT  2 fixtures/);
    assert.doesNotMatch(text, /safe/i);
    assert.doesNotMatch(text, /unsafe/i);
    assert.doesNotMatch(text, /p50/i);
    assert.doesNotMatch(text, /p95/i);
  });

  it("renders from existing live-refresh polls without extra fetch, scan, or interval", () => {
    const display = readFileSync(join(frontendRoot, "lib/system-load-display.ts"), "utf8");
    const card = readFileSync(join(frontendRoot, "components/system-load-summary.tsx"), "utf8");
    const bar = readFileSync(join(frontendRoot, "components/venue-health-bar.tsx"), "utf8");
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    assert.match(bar, /SystemLoadSummaryCard/);
    assert.match(scan, /SystemLoadSummaryCard/);
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
