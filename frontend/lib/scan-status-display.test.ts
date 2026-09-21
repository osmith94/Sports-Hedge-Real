import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { LaneRefreshStatus, LiveRefreshStatus } from "./api";
import { BACKGROUND_PRICING_LABEL, HOT_PRICING_LABEL, UNIVERSE_DISCOVERY_LABEL, backgroundPriceCopy, dualScanStatusLines, fastScanCopy, fullSweepCopy } from "./scan-status-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function status(
  overrides: Omit<Partial<LiveRefreshStatus>, "hot" | "universe"> & {
    hot?: Partial<LaneRefreshStatus>;
    universe?: Partial<LaneRefreshStatus>;
  } = {},
): LiveRefreshStatus {
  const { hot, universe, ...rest } = overrides;
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
      ...hot,
    },
    universe: {
      cadence_seconds: 600,
      generation_budget_seconds: 150,
      generation_work_used_s: 41,
      chunk_last_duration_ms: 8000,
      fixture_count: 104,
      evaluated_count: 60,
      not_evaluated_count: 44,
      ...universe,
    },
    ...rest,
  };
}

describe("dual cadence operator copy", () => {
  it("renders HOT pricing, BACKGROUND pricing and UNIVERSE discovery as distinct lines", () => {
    const now = Date.parse("2026-09-14T12:00:12Z");
    const lines = dualScanStatusLines(status(), now);
    assert.equal(lines.length, 4);
    assert.match(lines[0], /ACTIVE TRADE/);
    assert.match(lines[1], /HOT pricing/);
    assert.match(lines[2], /BACKGROUND pricing/);
    assert.match(lines[3], /UNIVERSE discovery/);
    assert.doesNotMatch(lines.join(" "), /Fast scan|Full sweep|Fast Scan|Full Sweep/);
    assert.doesNotMatch(lines.join(" "), /^Last scan /);
    assert.match(fastScanCopy(status(), now).detail, /completed 12s ago/);
    assert.match(fastScanCopy(status(), now).detail, /next due in 6s/);
    assert.match(fastScanCopy(status(), now).detail, /ran 4.1s/);
    assert.match(fastScanCopy(status(), now).detail, /partial \(2 not evaluated\)/);
    assert.doesNotMatch(fastScanCopy(status(), now).detail, /scan_cycle_timeout/);
    assert.match(fullSweepCopy(status(), now).detail, /104 universe/);
    assert.match(fullSweepCopy(status(), now).detail, /cadence 600s/);
    assert.doesNotMatch(fullSweepCopy(status(), now).detail, /chunk/i);
    assert.doesNotMatch(fullSweepCopy(status(), now).detail, /HOT next due/);
    assert.doesNotMatch(fullSweepCopy(status(), now).detail, /until HOT/i);
    const withBackground = status({
      background: {
        cadence_seconds: 90,
        next_due_at: "2026-09-14T12:01:42Z",
      },
      price_engine: {
        background: {
          working_set: 12,
          evaluated: 4,
          in_flight: 1,
          retry_wait: 2,
          deferred: 0,
          not_started_this_cadence: 5,
        },
      },
    });
    const backgroundLines = dualScanStatusLines(withBackground, now);
    assert.equal(backgroundLines.length, 4);
    assert.match(backgroundLines[0], /ACTIVE TRADE/);
    assert.match(backgroundLines[2], /BACKGROUND pricing/);
    assert.match(backgroundLines[2], /12 ACTIVE/);
    assert.match(backgroundPriceCopy(withBackground, now)?.detail || "", /4 evaluated/);
    assert.match(backgroundPriceCopy(withBackground, now)?.detail || "", /cadence 90s/);
    assert.match(backgroundPriceCopy(withBackground, now)?.detail || "", /next due in 90s/);
    assert.equal(fastScanCopy(status(), now).label, HOT_PRICING_LABEL);
    assert.equal(fullSweepCopy(status(), now).label, UNIVERSE_DISCOVERY_LABEL);
    assert.equal(backgroundPriceCopy(status(), now).label, BACKGROUND_PRICING_LABEL);
    const completedUniverse = status({
      universe: {
        cadence_seconds: 600,
        worker_state: "complete",
        next_due_at: "2026-09-14T12:10:12Z",
      },
    });
    assert.match(fullSweepCopy(completedUniverse, now).detail, /next due in 600s/);
    assert.match(fullSweepCopy(completedUniverse, now).detail, /cadence 600s/);
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
        venue_health: { matchbook: "ok", kalshi: "ok" },
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
        venue_health: { matchbook: "ok", polymarket: "ok", kalshi: "ok" },
      },
      venue_health: { matchbook: "ok", polymarket: "ok", kalshi: "ok" },
    });
    assert.match(fastScanCopy(withVenues, now).detail, /last scan MB·K/);
    assert.doesNotMatch(fastScanCopy(withVenues, now).detail, /PM/);
    assert.match(fullSweepCopy(withVenues, now).detail, /last scan MB·PM·K/);
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
        venue_health: { matchbook: "ok", kalshi: "ok" },
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
        venue_health: { matchbook: "ok", polymarket: "ok" },
      },
      venue_health: { matchbook: "ok", kalshi: "ok", polymarket: "ok" },
    });
    assert.match(fastScanCopy(combined, now).detail, /last scan MB·K/);
    assert.match(fastScanCopy(combined, now).detail, /persist\/auto-capture failed/);
    assert.match(fastScanCopy(combined, now).detail, /partial \(2 not evaluated\)/);
    assert.doesNotMatch(fastScanCopy(combined, now).detail, /scan_cycle_timeout/);
    assert.match(fullSweepCopy(combined, now).detail, /last scan MB·PM/);
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
    assert.match(scan, /applyLatestLiveRefresh/);
    assert.doesNotMatch(scan, /void collectRef\.current\(\)/);
    assert.match(bar, /AUTO PAPER CAPTURE ON/);
    assert.match(bar, /paper_autofill_enabled/);
    assert.match(scan, /AUTO PAPER CAPTURE ON/);
    assert.match(layout, /PAPER MODE · NO EXECUTION/);
  });

  it("distinguishes HOT worker alive with empty scope from never scheduled", () => {
    const never = status({
      hot: {
        cadence_seconds: 30,
        cycle_timeout_seconds: 25,
        last_completed_at: null,
        last_started_at: null,
        last_heartbeat_at: null,
        last_plan_reason: null,
        fixture_count: 0,
        not_evaluated_count: 0,
      },
    });
    assert.equal(fastScanCopy(never).detail, "never");

    const idleAlive = status({
      hot: {
        cadence_seconds: 30,
        cycle_timeout_seconds: 25,
        last_completed_at: null,
        last_started_at: null,
        last_heartbeat_at: "2026-09-18T12:00:00Z",
        last_plan_reason: "hot_scope_empty",
        worker_state: "waiting",
        fixture_count: 0,
        not_evaluated_count: 0,
      },
    });
    assert.match(fastScanCopy(idleAlive).detail, /worker alive/);
    assert.match(fastScanCopy(idleAlive).detail, /scope empty/);
    assert.doesNotMatch(fastScanCopy(idleAlive).detail, /never/);
  });

  it("uses canonical work counts so three venue aliases stay 1/1 remaining 0", () => {
    const now = Date.parse("2026-09-14T12:00:12Z");
    const canonical = status({
      universe: {
        cadence_seconds: 180,
        generation_budget_seconds: 150,
        fixture_count: 1,
        evaluated_count: 1,
        discovered_total: 1,
        not_evaluated_count: 2,
        remaining: 0,
        canonical_work_total: 1,
        canonical_evaluated: 1,
        canonical_remaining: 0,
        worker_state: "complete",
      },
    });
    assert.match(fullSweepCopy(canonical, now).detail, /1 evaluated \/ 0 not evaluated/);
    assert.doesNotMatch(fullSweepCopy(canonical, now).detail, /2 not evaluated/);
  });

  it("does not label UNIVERSE retry_wait as in progress", () => {
    const waiting = status({
      universe: {
        cadence_seconds: 600,
        generation_budget_seconds: 150,
        cycle_in_progress: false,
        worker_state: "waiting",
        last_plan_reason: "universe_retry_wait",
        last_heartbeat_at: "2026-09-18T22:40:00Z",
        fixture_count: 41,
        evaluated_count: 72,
        canonical_work_total: 113,
        canonical_evaluated: 72,
        canonical_remaining: 41,
        canonical_retryable: 6,
      },
    });
    assert.doesNotMatch(fullSweepCopy(waiting).detail, /in progress/);
    assert.match(fullSweepCopy(waiting).detail, /waiting/);
    assert.match(fullSweepCopy(waiting).detail, /72 evaluated/);
  });

  it("makes operator-stopped status explicit for ACTIVE TRADE/HOT/BACKGROUND/UNIVERSE", () => {
    const lines = dualScanStatusLines(
      status({
        scanner_stopped: true,
        background: { cadence_seconds: 90, cycle_timeout_seconds: null },
      }),
    );
    assert.equal(lines.length, 4);
    assert.match(lines[0], /ACTIVE TRADE/);
    assert.match(lines[1], /HOT pricing/);
    assert.match(lines[2], /BACKGROUND pricing/);
    assert.match(lines[3], /UNIVERSE discovery/);
    assert.match(lines[0], /stopped by operator/);
    assert.match(lines[1], /stopped by operator/);
    assert.match(lines[2], /stopped by operator/);
    assert.match(lines[3], /stopped by operator/);
    assert.doesNotMatch(lines.join(" "), /Fast scan|Full sweep|Fast Scan|Full Sweep/);
  });

  it("says Paused for UNIVERSE instead of a ticking next-due countdown", () => {
    const now = Date.parse("2026-09-21T12:00:10Z");
    const paused = status({
      universe_scans_paused: true,
      universe: {
        cadence_seconds: 1800,
        next_due_at: null,
        last_plan_reason: "universe_scheduled_paused",
        worker_state: "waiting",
        fixture_count: 104,
        evaluated_count: 60,
        not_evaluated_count: 44,
      },
      background: {
        cadence_seconds: 90,
        next_due_at: "2026-09-21T12:01:40Z",
      },
    });
    assert.match(fullSweepCopy(paused, now).detail, /^Paused/);
    assert.match(fullSweepCopy(paused, now).detail, /cadence 1800s/);
    assert.doesNotMatch(fullSweepCopy(paused, now).detail, /next due in/);
    const lines = dualScanStatusLines(paused, now);
    assert.match(lines[1], /HOT pricing/);
    assert.doesNotMatch(lines[1], /Paused/);
    assert.match(lines[1], /next due in/);
    assert.match(lines[2], /BACKGROUND pricing/);
    assert.match(lines[2], /next due in/);
    assert.match(lines[3], /UNIVERSE discovery · Paused/);
  });

  it("routes primary Manual HOT refresh to HOT and labels full discovery as advanced Full diagnostic", () => {
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    const api = readFileSync(join(frontendRoot, "lib/api.ts"), "utf8");
    const modal = readFileSync(join(frontendRoot, "components/football-competitions-modal.tsx"), "utf8");
    assert.match(scan, /await collect\("hot"\)/);
    assert.match(scan, /runPaperHotRefresh\(payload\)/);
    assert.match(scan, /Manual HOT refresh/);
    assert.match(scan, /Manual BACKGROUND refresh/);
    assert.match(scan, /Run UNIVERSE now/);
    assert.match(scan, /Football competitions/);
    assert.match(scan, /Run full diagnostic/);
    assert.doesNotMatch(scan, /Run scan/);
    assert.doesNotMatch(scan, /Fast [Ss]can|Full [Ss]weep/);
    assert.doesNotMatch(scan, /collect\("universe"\)/);
    assert.match(scan, /disabled=\{loading \|\| scannerStopped\}/);
    assert.match(scan, /if \(liveRefresh\?\.scanner_stopped\) return;/);
    assert.match(scan, /collect\("diagnostic"\)/);
    assert.match(scan, /does not\s+rediscover the catalogue/);
    assert.match(scan, /is not UNIVERSE discovery/);
    assert.match(scan, /HOT cadence s/);
    assert.match(scan, /BACKGROUND cadence s/);
    assert.match(scan, /UNIVERSE cadence s/);
    assert.match(scan, /background_cadence_seconds/);
    assert.match(scan, /universe_cadence_seconds/);
    assert.match(scan, /clampBackgroundCadenceSeconds/);
    assert.match(scan, /clampUniverseCadenceSeconds/);
    assert.match(scan, /Min net arb %/);
    assert.match(scan, /Outright Min net arb %/);
    assert.match(scan, /placeholder="not set"/);
    assert.match(scan, /outright_min_net_edge: outrightMinNet \?\? null/);
    assert.match(scan, /scan-field-outright/);
    assert.doesNotMatch(scan, /DEFAULT_OUTRIGHT/);
    assert.match(scan, /const \[outrightMinNetArbPercent, setOutrightMinNetArbPercent\] = useState\(""\)/);
    assert.match(scan, /HOT cadence, BACKGROUND cadence and UNIVERSE cadence/);
    assert.match(scan, /DEFAULT_UNIVERSE_CADENCE_SECONDS = 1800/);
    assert.match(scan, /Update/);
    assert.match(scan, /Stop scanner/);
    assert.match(scan, /Resume scanner/);
    assert.match(scan, /DEFAULT_MIN_NET_ARB_PERCENT = "1.00"/);
    assert.match(scan, /DEFAULT_MAX_RISK = "60"/);
    assert.match(scan, /DEFAULT_HOT_CADENCE_SECONDS = 30/);
    assert.match(scan, /DEFAULT_MAX_ALLOCATED_PER_TRADE_GBP = "1000"/);
    assert.match(scan, /useState\(DEFAULT_MIN_NET_ARB_PERCENT\)/);
    assert.match(scan, /useState\(DEFAULT_MAX_RISK\)/);
    assert.match(scan, /useState\(DEFAULT_MAX_ALLOCATED_PER_TRADE_GBP\)/);
    assert.match(scan, /max_allocated_per_trade_gbp: allocated/);
    assert.match(scan, /ACTIVE TRADE \/ HOT pricing \/ BACKGROUND pricing \/ UNIVERSE discovery paused/);
    const pulse = readFileSync(join(frontendRoot, "components/live-scan-pulse.tsx"), "utf8");
    assert.match(
      pulse,
      /ACTIVE TRADE \/ HOT pricing \/ BACKGROUND pricing \/ UNIVERSE discovery paused/,
    );
    assert.match(scan, /Pause scheduled UNIVERSE/);
    assert.match(scan, /Resume scheduled UNIVERSE/);
    assert.match(scan, /pauseUniverseSchedule/);
    assert.match(scan, /resumeUniverseSchedule/);
    assert.match(scan, /UNIVERSE SCHEDULE PAUSED/);
    assert.match(scan, /stopPaperScanner/);
    assert.match(scan, /resumePaperScanner/);
    assert.match(scan, /How scanning works/);
    assert.match(scan, /<details className="scan-help">/);
    assert.match(scan, /status-badge status-badge-stopped/);
    assert.match(api, /\/paper\/scanner\/universe-schedule\/pause/);
    assert.match(api, /\/paper\/scanner\/universe-schedule\/resume/);
    assert.doesNotMatch(scan, /Refresh interval/);
    assert.match(api, /\/paper\/collect\/hot/);
    assert.match(api, /\/paper\/collect\/background/);
    assert.match(api, /\/paper\/collect\/universe/);
    assert.match(api, /\/paper\/universe-scope/);
    assert.match(api, /PAPER_HOT_REFRESH_TIMEOUT_MS = 35_000/);
    assert.match(api, /\/paper\/collect`/);
    assert.match(modal, /Discovery scope/);
    assert.match(modal, /Apply & Run UNIVERSE now/);
    assert.match(modal, /Select defaults/);
    assert.match(modal, /Select all supported/);
    assert.match(modal, /Save this selection as my default/);
    assert.match(modal, /Restore saved default/);
    assert.doesNotMatch(modal, /KXUCL|KXEPLGAME|series_ticker/);
  });
});
