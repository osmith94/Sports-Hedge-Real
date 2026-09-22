import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { LiveRefreshStatus, SystemLoadSummary } from "./api";
import { systemLoadDetailLines, systemLoadLines } from "./system-load-display";

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
    background: {
      working_set: 31,
      due: 6,
      cadence_seconds: 90,
    },
    universe: {
      evaluated: 24,
      total: 30,
      remaining: 6,
      cadence_seconds: 600,
      generation_work_used_s: 42,
      generation_budget_seconds: 150,
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
    const lines = systemLoadLines(load({
      hot: {
        fixtures: 8,
        pricing_fixtures: 0,
        working_set: 18,
        due: 4,
        cadence_seconds: 30,
        health: "healthy",
      },
      background: { working_set: 29, pricing_fixtures: 29, due: 6, cadence_seconds: 60, health: "healthy" },
      universe: {
        evaluated: 16,
        total: 759,
        remaining: 743,
        cadence_seconds: 600,
        generation_budget_seconds: 150,
        worker_state: "running",
        health: "running",
      },
      active_trade: { open_trades: 4, due: 0, overdue: 0, cadence_seconds: 5, capital_locked_gbp: "58.95" },
    }));
    assert.deepEqual(
      lines.map((line) => `${line.key}  ${line.detail}`),
      [
        "HOT  0 fixtures · healthy · 30s",
        "BACKGROUND  29 fixtures · healthy · 60s",
        "UNIVERSE  16/759 · running · 150s chunk",
        "ACTIVE TRADES  4 open · £58.95 locked",
      ],
    );
    const detail = systemLoadDetailLines(load());
    assert.match(detail.map((line) => line.detail).join(" "), /8 fixtures · 18 items/);
    assert.match(detail.map((line) => `${line.key} ${line.detail}`).join(" "), /MB/);
  });

  it("labels generation work above the chunk wall as cumulative, not a blown budget", () => {
    const universe = systemLoadDetailLines(
      load({
        universe: {
          evaluated: 16,
          total: 761,
          remaining: 745,
          cadence_seconds: 1800,
          generation_work_used_s: 946,
          generation_budget_seconds: 150,
        },
      }),
    ).find((line) => line.key === "UNIVERSE discovery");
    assert.match(universe?.detail ?? "", /946s cumulative \/ 150s chunk/);
  });

    it("appends wait, service latency, deadline misses and saturation without p50 language", () => {
    const lines = systemLoadDetailLines(
      load({
        matchbook: {
          inflight: 4,
          limit: 4,
          waiting: 3,
          wait_ms: 1800,
          latency_ms: 220,
          deadline_misses: 2,
          saturated: true,
        },
      }),
    );
    assert.match(lines[4].detail, /4\/4 in use · queue 3 · wait 1.8s · svc 220ms · 2 deadline misses · saturated/);
    const text = lines.map((line) => line.detail).join(" ");
    assert.doesNotMatch(text, /p50/i);
    assert.doesNotMatch(text, /p95/i);
    assert.doesNotMatch(text, /safe/i);
    assert.doesNotMatch(text, /unsafe/i);
  });

  it("keeps fixture count distinct from catalogue items using backend fields", () => {
    const lines = systemLoadDetailLines(
      load({
        hot: { fixtures: 1, working_set: 3, due: 3, last_cycle_ms: 1200, cadence_seconds: 30 },
        catalogue_items: 3,
      }),
    );
    assert.match(lines[1].detail, /1 fixtures · 3 items/);
    assert.equal(lines[6].detail, "3 catalogue items");
    const compact = systemLoadLines(
      load({
        hot: { fixtures: 12, pricing_fixtures: 1, working_set: 3, cadence_seconds: 30 },
      }),
    );
    assert.match(compact[0].detail, /1 fixtures/);
    assert.doesNotMatch(compact[0].detail, /12 fixtures/);
  });

  it("displays backend missing cycle duration without computing utilisation", () => {
    const missing = systemLoadDetailLines(
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
    assert.match(missing[1].detail, /cycle — \/ 30s/);
    assert.doesNotMatch(missing[1].detail, /NaN/);
    assert.doesNotMatch(missing[1].detail, /%\)/);
  });

  it("exposes ACTIVE TRADE overdue count only when capacity is late", () => {
    const hidden = systemLoadLines(load({ active_trade: { open_trades: 2, due: 2, overdue: 0, cadence_seconds: 5 } }));
    assert.equal(hidden[3].detail, "2 open");
    assert.doesNotMatch(hidden[3].detail, /overdue/);
    const late = systemLoadLines(load({ active_trade: { open_trades: 2, due: 2, overdue: 1, cadence_seconds: 5 } }));
    assert.equal(late[3].detail, "2 open · 1 overdue");
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
    assert.match(card, /system-load-key/);
    assert.match(card, /system-load-detail/);
    const css = readFileSync(join(frontendRoot, "app/globals.css"), "utf8");
    assert.match(css, /\.system-load-key \{ font-weight: 750; color: var\(--text\); white-space: nowrap; \}/);
    assert.doesNotMatch(css, /system-load-key \{[^}]*width: 2\.6em/);
    assert.match(css, /\.status-badge-stopped/);
    assert.match(css, /\.live-scan-pulse-stopped \.live-scan-pulse-core \{ background: var\(--muted-2\); \}/);
    const pools = readFileSync(join(frontendRoot, "components/liquidity-pools.tsx"), "utf8");
    assert.match(pools, /treasury-carrying-value/);
    assert.match(pools, /treasury-carrying-source/);
    assert.match(pools, /title=\{carryingFromTreasury\(pool\)\}/);
    assert.match(css, /\.treasury-carrying-value/);
  });
});
