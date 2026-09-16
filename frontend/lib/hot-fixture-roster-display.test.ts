import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { DiscoveredFixture, LiveRefreshStatus } from "./api";
import {
  HOT_REASON_ARB_PROMOTION,
  HOT_REASON_IN_PLAY,
  HOT_REASON_KICKOFF_HORIZON,
  HOT_REASON_POST_KICKOFF_STATUS_PENDING,
  HOT_ROSTER_EMPTY,
  HOT_ROSTER_TITLE,
  HOT_ROSTER_UNAVAILABLE,
  HOT_ZONE_KICKER,
  fastScanRosterSummary,
  hotEquivalentLabel,
  hotEvaluationLabel,
  hotFixtureRow,
  hotFixtureRows,
  hotFixtures,
  hotNetEdgeLabel,
  hotReasonLabels,
  hotRosterBadgeLabel,
  hotVenuePresenceLabel,
} from "./hot-fixture-roster-display";
import { opportunityMonitorRows } from "./opportunity-monitor-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function fixture(overrides: Partial<DiscoveredFixture> = {}): DiscoveredFixture {
  return {
    source: "matchbook",
    source_event_id: "mb-1",
    canonical_event_id: "evt/hot-1",
    home_team: "Leeds United",
    away_team: "Newcastle United",
    competition: "Premier League",
    kickoff_utc: "2026-09-16T19:00:00Z",
    polymarket_matched: false,
    live_score_supported: false,
    last_seen_at: "2026-09-16T18:10:00Z",
    matched_market_count: 0,
    solver_is_arbitrage: false,
    scan_lane: "hot",
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
    hot: { cadence_seconds: 30, fixture_count: 0, evaluated_count: 0 },
    universe: { cadence_seconds: 180, fixture_count: 0 },
    ...overrides,
  };
}

describe("HOT fixture roster membership", () => {
  it("renders HOT rows when scan_lane is hot even with no arb and no watchlist row", () => {
    const live = status({
      discovered_fixtures: [
        fixture({
          solver_is_arbitrage: false,
          current_net_edge: null,
          matched_equivalent_count: 0,
          hot_reasons: [HOT_REASON_IN_PLAY],
          in_running: true,
        }),
      ],
    });
    const rows = hotFixtureRows(live);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].name, "Leeds United v Newcastle United");
    assert.equal(rows[0].hasQualifyingOpportunity, false);
    assert.deepEqual(opportunityMonitorRows([]), []);
  });

  it("excludes UNIVERSE rows from the HOT panel", () => {
    const live = status({
      discovered_fixtures: [
        fixture({ canonical_event_id: "evt/hot", scan_lane: "hot", hot_reasons: [HOT_REASON_IN_PLAY] }),
        fixture({
          canonical_event_id: "evt/universe",
          scan_lane: "universe",
          kickoff_utc: "2026-09-20T15:00:00Z",
          hot_reasons: [],
        }),
        fixture({ canonical_event_id: "evt/missing-lane", scan_lane: null }),
      ],
    });
    assert.deepEqual(
      hotFixtures(live).map((item) => item.canonical_event_id),
      ["evt/hot"],
    );
  });

  it("keeps an honest empty HOT state without fabricating fixtures", () => {
    assert.equal(hotRosterBadgeLabel(true, status()), "EMPTY");
    assert.equal(hotFixtureRows(status()).length, 0);
    assert.equal(hotRosterBadgeLabel(false, null), "UNAVAILABLE");
    assert.match(HOT_ROSTER_EMPTY, /Fast Scan roster is empty/i);
    assert.match(HOT_ROSTER_UNAVAILABLE, /No fabricated fixtures/);
  });
});

describe("HOT reason presentation", () => {
  it("shows IN PLAY from the backend read-model field", () => {
    const row = hotFixtureRow(
      fixture({
        in_running: true,
        hot_reasons: [HOT_REASON_IN_PLAY],
      }),
    );
    assert.deepEqual(row.reasons, [HOT_REASON_IN_PLAY]);
    assert.ok(row.kickoffLines.some((line) => line.startsWith("LIVE")));
  });

  it("shows pre-kickoff HOT reason inside the configured horizon", () => {
    const row = hotFixtureRow(
      fixture({
        in_running: false,
        hot_reasons: [HOT_REASON_KICKOFF_HORIZON],
      }),
    );
    assert.deepEqual(row.reasons, [HOT_REASON_KICKOFF_HORIZON]);
  });

  it("labels qualifying-opportunity promotion without claiming every HOT fixture is an arb", () => {
    const live = status({
      discovered_fixtures: [
        fixture({
          canonical_event_id: "evt/in-play",
          in_running: true,
          solver_is_arbitrage: false,
          hot_reasons: [HOT_REASON_IN_PLAY],
        }),
        fixture({
          canonical_event_id: "evt/promoted",
          scan_lane: "hot",
          solver_is_arbitrage: true,
          current_net_edge: "0.014",
          hot_reasons: [HOT_REASON_ARB_PROMOTION],
        }),
      ],
    });
    const rows = hotFixtureRows(live);
    const inPlay = rows.find((row) => row.id === "evt/in-play");
    const promoted = rows.find((row) => row.id === "evt/promoted");
    assert.deepEqual(inPlay?.reasons, [HOT_REASON_IN_PLAY]);
    assert.equal(inPlay?.hasQualifyingOpportunity, false);
    assert.ok(!inPlay?.reasons.includes(HOT_REASON_ARB_PROMOTION));
    assert.deepEqual(promoted?.reasons, [HOT_REASON_ARB_PROMOTION]);
    assert.equal(promoted?.hasQualifyingOpportunity, true);
  });

  it("does not invent kickoff or arb-promotion reasons when the read-model field is absent", () => {
    assert.deepEqual(
      hotReasonLabels(
        fixture({
          in_running: false,
          solver_is_arbitrage: true,
          hot_reasons: undefined,
        }),
      ),
      [],
    );
    assert.deepEqual(
      hotReasonLabels(fixture({ in_running: true, hot_reasons: undefined })),
      [HOT_REASON_IN_PLAY],
    );
    assert.deepEqual(
      hotReasonLabels(
        fixture({
          hot_reasons: [HOT_REASON_POST_KICKOFF_STATUS_PENDING],
        }),
      ),
      [HOT_REASON_POST_KICKOFF_STATUS_PENDING],
    );
  });
});

describe("HOT roster honesty for missing venue, equivalent and edge", () => {
  it("does not fabricate venue presence, equivalent count or net edge", () => {
    const row = hotFixtureRow(
      fixture({
        matchbook_matched: undefined,
        polymarket_matched: false,
        kalshi_matched: undefined,
        matched_equivalent_count: null,
        current_net_edge: null,
        hot_reasons: [HOT_REASON_KICKOFF_HORIZON],
      }),
    );
    assert.equal(hotVenuePresenceLabel(fixture({ matchbook_matched: true, polymarket_matched: true, kalshi_matched: false })), "MB / PM / —");
    assert.equal(row.venuesLabel, "— / — / —");
    assert.equal(row.equivalentLabel, "—");
    assert.equal(row.netEdgeLabel, "—");
    assert.equal(hotEquivalentLabel(fixture({ matched_equivalent_count: 0 })), "0");
    assert.equal(hotNetEdgeLabel(fixture({ current_net_edge: "0.012" })), "1.20%");
  });
});

describe("HOT roster console placement", () => {
  it("places HOT Fixtures / Fast Scan after discovery and before Opportunity Monitor", () => {
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const panel = readFileSync(join(frontendRoot, "components/hot-fixtures-panel.tsx"), "utf8");
    const css = readFileSync(join(frontendRoot, "app/globals.css"), "utf8");
    const discoveryIndex = page.indexOf("<FixtureDiscoverySection");
    const hotIndex = page.indexOf("<HotFixturesPanel");
    const monitorIndex = page.indexOf("<OpportunityMonitor");
    assert.ok(discoveryIndex >= 0);
    assert.ok(hotIndex > discoveryIndex);
    assert.ok(monitorIndex > hotIndex);
    assert.match(page, /from "\.\.\/components\/hot-fixtures-panel"/);
    assert.equal(HOT_ROSTER_TITLE, "HOT Fixtures / Fast Scan");
    assert.equal(HOT_ZONE_KICKER, "HOT Zone");
    assert.match(panel, /HOT_ZONE_KICKER/);
    assert.match(panel, /HOT_ROSTER_TITLE/);
    assert.match(panel, /fastScanRosterSummary/);
    assert.match(panel, /hotFixtureRows/);
    assert.match(css, /hot-zone-panel/);
    assert.doesNotMatch(panel, /getTrackedWatchlist/);
    assert.doesNotMatch(panel, /DEMO_/);
    assert.doesNotMatch(panel, /getPaperScans/);
  });

  it("does not repurpose Opportunity Monitor into HOT inventory", () => {
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const monitor = readFileSync(join(frontendRoot, "components/opportunity-monitor.tsx"), "utf8");
    assert.match(page, /items=\{tracked\.available \? tracked\.value : \[\]\}/);
    assert.doesNotMatch(page, /<OpportunityMonitor[\s\S]*discovered_fixtures/);
    assert.match(monitor, /Current radar set from tracked watchlist/);
  });
});

describe("HOT Zone evaluation state and Fast Scan summary", () => {
  it("shows current evaluation state and reason on HOT rows with no opportunity", () => {
    const evaluated = hotFixtureRow(
      fixture({
        solver_is_arbitrage: false,
        current_net_edge: null,
        market_evaluation_state: "evaluated",
        no_comparison_reason: "no_comparable_markets",
        hot_reasons: [HOT_REASON_IN_PLAY],
      }),
    );
    assert.equal(evaluated.hasQualifyingOpportunity, false);
    assert.equal(evaluated.evaluationLabel, "evaluated · no_comparable_markets");
    assert.equal(
      hotEvaluationLabel(
        fixture({
          market_evaluation_state: "not_evaluated_scan_deadline",
          market_evaluation_reason: "scan_budget_exhausted",
        }),
      ),
      "Not evaluated — scan budget exhausted · scan_budget_exhausted",
    );
    assert.equal(
      hotEvaluationLabel(fixture({ solver_is_arbitrage: true, market_evaluation_state: "evaluated" })),
      "qualifying",
    );
  });

  it("summarizes Fast Scan from truthful HOT lane fields", () => {
    const live = status({
      last_paper_decisions: 99,
      last_completed_at: "2026-09-16T18:05:00Z",
      discovered_fixtures: [fixture()],
      hot: {
        cadence_seconds: 30,
        fixture_count: 4,
        evaluated_count: 3,
        last_completed_at: "2026-09-16T18:05:00Z",
        last_diagnostics: { paper_decision_count: 2 },
      },
      universe: { cadence_seconds: 180, last_completed_at: "2026-09-16T18:04:00Z" },
    });
    assert.equal(fastScanRosterSummary(live), "4 HOT · 3 evaluated · 2 paper decisions");
    assert.equal(fastScanRosterSummary(status()), "0 HOT · 0 evaluated · — paper decisions");
  });
});
