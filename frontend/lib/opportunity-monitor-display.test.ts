import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { LiveRefreshStatus, NearOpportunity } from "./api";
import { fixtureDetailHref } from "./discovered-fixture-display";
import { formatObservationAge, OBSERVATION_AGE_TICK_MS } from "./observation-age";
import {
  MAPPING_UNAVAILABLE_TITLE,
  compareDefaultOpportunityOrder,
  formatOpportunityLegLine,
  initialOpportunityMonitorDirection,
  mappingDisplay,
  nextOpportunityMonitorSort,
  opportunityLegViews,
  opportunityMonitorRow,
  opportunityMonitorRows,
  opportunityMonitorState,
  opportunityMonitorSummary,
  opportunityObservationTimestamp,
  scanLaneLabel,
  sortOpportunityMonitor,
} from "./opportunity-monitor-display";
import { shouldNavigateFromRowClick, trackedMarketHref } from "./tracked-markets-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function watch(
  overrides: Partial<NearOpportunity> & Pick<NearOpportunity, "opportunity_id">,
): NearOpportunity {
  const { opportunity_id, ...rest } = overrides;
  return {
    opportunity_id,
    canonical_event_id: "evt/leeds-newcastle",
    canonical_market_id: "mkt-1",
    home_team: "Leeds United",
    away_team: "Newcastle United",
    competition: "Premier League",
    market_family: "match_result",
    period: "full_time",
    venues: ["matchbook", "kalshi"],
    legs: [
      {
        outcome: "home",
        venue: "matchbook",
        source_market_id: "mb-1",
        currency: "GBP",
        net_decimal_odds: 2.1,
        cumulative_depth_gbp: 120,
      },
      {
        outcome: "draw",
        venue: "matchbook",
        source_market_id: "mb-2",
        currency: "GBP",
        net_decimal_odds: 3.4,
        cumulative_depth_gbp: 80,
      },
      {
        outcome: "away",
        venue: "kalshi",
        source_market_id: "k-1",
        currency: "USD",
        net_decimal_odds: 4.2,
        cumulative_depth_gbp: 60,
      },
    ],
    status: "WATCHING",
    classification: "near_opportunity",
    trigger_net_edge: 0.01,
    current_net_edge: 0.008,
    gross_edge: 0.012,
    first_seen_at: "2026-09-15T12:00:00.000Z",
    last_seen_at: "2026-09-15T12:00:20.000Z",
    last_scanned_at: "2026-09-15T12:00:18.000Z",
    scan_lane: "hot",
    freshness_class: "radar_current",
    rejection_reasons: [],
    insufficiency_reasons: [],
    ...rest,
  };
}

function refresh(overrides: Partial<LiveRefreshStatus> = {}): LiveRefreshStatus {
  return {
    discovery_source: "matchbook",
    server_loop_enabled: true,
    interval_seconds: 30,
    cycle_in_progress: false,
    live_scores: "unavailable",
    discovered_fixtures: [],
    hot: {
      cadence_seconds: 30,
      active_venues: ["matchbook", "polymarket"],
      last_completed_at: "2026-09-15T12:00:00.000Z",
      last_duration_ms: 4000,
      next_due_at: "2026-09-15T12:00:30.000Z",
      fixture_count: 4,
    },
    universe: {
      cadence_seconds: 180,
      active_venues: ["matchbook", "kalshi"],
      fixture_count: 40,
      evaluated_count: 20,
      not_evaluated_count: 20,
    },
    ...overrides,
  };
}

describe("opportunity monitor current-vs-audit separation", () => {
  const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
  const monitor = readFileSync(join(frontendRoot, "components/opportunity-monitor.tsx"), "utf8");

  it("drives the primary table from tracked radar and keeps audit collapsed and distinct", () => {
    assert.match(page, /getTrackedWatchlist\("limit=100"\)/);
    assert.match(page, /<OpportunityMonitor/);
    assert.match(page, /items=\{tracked\.available \? tracked\.value : \[\]\}/);
    assert.match(page, /getPaperScans\("limit=100"\)/);
    assert.match(page, /audit-disclosure/);
    assert.match(page, /LATEST 100 AUDIT/);
    assert.match(page, /Latest 100 audit observations/);
    assert.match(page, /Not current scanner radar/);
    assert.match(page, /<PaperScanHistoryTable scans=\{scans\}/);
    assert.doesNotMatch(page, /panel-title">Paper scan history/);
    assert.doesNotMatch(page, /<span>Tracked<\/span>/);
    assert.doesNotMatch(page, /TrackedMarketsBoard/);
    const hotIdx = page.indexOf("<HotFixturesPanel");
    const monitorIdx = page.indexOf("<OpportunityMonitor");
    const positionsIdx = page.indexOf("Open paper positions");
    const auditIdx = page.indexOf("audit-disclosure");
    assert.ok(hotIdx > 0 && monitorIdx > hotIdx && positionsIdx > monitorIdx && auditIdx > positionsIdx);
    assert.match(monitor, /Current radar set from tracked watchlist/);
    assert.doesNotMatch(monitor, /getPaperScans/);
    assert.doesNotMatch(monitor, /DEMO_NEAR_ARB/);
    assert.doesNotMatch(page, /DEMO_NEAR_ARB/);
  });
});

describe("opportunity monitor state badges", () => {
  it("maps canonical watchlist states onto compact badges without using rejection prose as the badge", () => {
    assert.equal(
      opportunityMonitorState(
        watch({
          opportunity_id: "q",
          status: "TRIGGERED",
          classification: "triggered_opportunity",
          is_arbitrage: true,
          freshness_class: "executable",
          bet_actionable: true,
        }),
      ),
      "QUALIFYING",
    );
    assert.equal(opportunityMonitorState(watch({ opportunity_id: "n" })), "NEAR");
    assert.equal(
      opportunityMonitorState(watch({ opportunity_id: "b", current_net_edge: -0.004 })),
      "BELOW BREAK-EVEN",
    );
    assert.equal(
      opportunityMonitorState(
        watch({
          opportunity_id: "r",
          status: "REJECTED",
          classification: "rejected",
          rejection_reasons: ["insufficient_depth", "missing_costs"],
        }),
      ),
      "REJECTED",
    );
    assert.equal(
      opportunityMonitorState(
        watch({
          opportunity_id: "s",
          freshness_class: "expired",
          rejection_reasons: ["stale_quote"],
        }),
      ),
      "STALE",
    );
    const rejected = opportunityMonitorRow(
      watch({
        opportunity_id: "r2",
        status: "REJECTED",
        classification: "rejected",
        rejection_reasons: ["insufficient_depth"],
      }),
    );
    assert.equal(rejected.state, "REJECTED");
    assert.match(rejected.stateTitle, /insufficient depth/);
    assert.notEqual(rejected.state, rejected.stateTitle);
  });

  it("labels a radar-current historical trigger STALE and excludes it from qualifying count", () => {
    const aged = watch({
      opportunity_id: "aged-trigger",
      status: "TRIGGERED",
      classification: "triggered_opportunity",
      is_arbitrage: true,
      freshness_class: "radar_current",
      bet_actionable: false,
      bet_blocked_reason: "radar_current_not_executable",
      quote_age_ms: 5000,
      guaranteed_profit_gbp: 1.24,
      current_net_edge: 0.021,
    });
    const live = watch({
      opportunity_id: "live-trigger",
      status: "TRIGGERED",
      classification: "triggered_opportunity",
      is_arbitrage: true,
      freshness_class: "executable",
      bet_actionable: true,
      quote_age_ms: 180,
      guaranteed_profit_gbp: 0.88,
      current_net_edge: 0.014,
    });
    const agedRow = opportunityMonitorRow(aged);
    const liveRow = opportunityMonitorRow(live);
    assert.equal(opportunityMonitorState(aged), "STALE");
    assert.notEqual(opportunityMonitorState(aged), "QUALIFYING");
    assert.equal(agedRow.state, "STALE");
    assert.match(agedRow.stateTitle, /not currently executable/);
    assert.match(agedRow.stateTitle, /radar current/);
    assert.equal(agedRow.netEdge, 0.021);
    assert.equal(agedRow.guaranteedProfitGbp, 1.24);
    assert.equal(agedRow.freshnessLabel, "radar current");
    assert.equal(opportunityMonitorState(live), "QUALIFYING");
    assert.equal(liveRow.state, "QUALIFYING");
    assert.doesNotMatch(liveRow.stateTitle, /not currently executable/);
    const unknownFreshness = opportunityMonitorState(
      watch({
        opportunity_id: "unknown-trigger",
        status: "TRIGGERED",
        classification: "triggered_opportunity",
        is_arbitrage: true,
        freshness_class: null,
        bet_actionable: false,
        quote_age_ms: 120,
      }),
    );
    assert.equal(unknownFreshness, "STALE");
    const summary = opportunityMonitorSummary([agedRow, liveRow], refresh(), true, true);
    assert.equal(summary.qualifyingCount, 1);
    assert.equal(summary.nearCount, 0);
  });

  it("does not treat executable PAPER_FILLING or PARTIAL as a new qualifying opportunity", () => {
    const filling = watch({
      opportunity_id: "filling",
      status: "PAPER_FILLING",
      classification: "paper_fill",
      is_arbitrage: true,
      freshness_class: "executable",
      bet_actionable: true,
      guaranteed_profit_gbp: 0.9,
      current_net_edge: 0.02,
    });
    const partial = watch({
      opportunity_id: "partial",
      status: "PARTIAL",
      classification: "paper_fill",
      is_arbitrage: true,
      freshness_class: "executable",
      bet_actionable: true,
      guaranteed_profit_gbp: 0.4,
      current_net_edge: 0.018,
    });
    const triggered = watch({
      opportunity_id: "live-trigger",
      status: "TRIGGERED",
      classification: "triggered_opportunity",
      is_arbitrage: true,
      freshness_class: "executable",
      bet_actionable: true,
      current_net_edge: 0.014,
    });
    const aged = watch({
      opportunity_id: "aged-trigger",
      status: "TRIGGERED",
      classification: "triggered_opportunity",
      is_arbitrage: true,
      freshness_class: "radar_current",
      bet_actionable: false,
      current_net_edge: 0.021,
    });
    assert.notEqual(opportunityMonitorState(filling), "QUALIFYING");
    assert.notEqual(opportunityMonitorState(partial), "QUALIFYING");
    assert.equal(opportunityMonitorState(triggered), "QUALIFYING");
    assert.equal(opportunityMonitorState(aged), "STALE");
    const rows = opportunityMonitorRows([filling, partial, triggered, aged]);
    assert.deepEqual(
      rows.map((row) => row.id),
      ["live-trigger", "aged-trigger"],
    );
    assert.equal(
      rows.filter((row) => row.state === "QUALIFYING").length,
      1,
    );
    const summary = opportunityMonitorSummary(rows, refresh(), true, true);
    assert.equal(summary.qualifyingCount, 1);
    assert.ok(!rows.some((row) => row.id === "filling" || row.id === "partial"));
  });
});

describe("opportunity monitor default ordering and user sort", () => {
  it("defaults to qualifying, then near, then other current, then highest net edge, then recency", () => {
    const rows = [
      opportunityMonitorRow(
        watch({
          opportunity_id: "stale",
          freshness_class: "expired",
          last_scanned_at: "2026-09-15T12:00:50.000Z",
          current_net_edge: 0.04,
        }),
      ),
      opportunityMonitorRow(
        watch({
          opportunity_id: "near-old",
          current_net_edge: 0.009,
          last_scanned_at: "2026-09-15T12:00:10.000Z",
        }),
      ),
      opportunityMonitorRow(
        watch({
          opportunity_id: "near-new",
          current_net_edge: 0.009,
          last_scanned_at: "2026-09-15T12:00:40.000Z",
        }),
      ),
      opportunityMonitorRow(
        watch({
          opportunity_id: "qual-low",
          status: "TRIGGERED",
          classification: "triggered_opportunity",
          is_arbitrage: true,
          freshness_class: "executable",
          bet_actionable: true,
          current_net_edge: 0.011,
          last_scanned_at: "2026-09-15T12:00:30.000Z",
        }),
      ),
      opportunityMonitorRow(
        watch({
          opportunity_id: "qual-high",
          status: "TRIGGERED",
          classification: "triggered_opportunity",
          is_arbitrage: true,
          freshness_class: "executable",
          bet_actionable: true,
          current_net_edge: 0.03,
          last_scanned_at: "2026-09-15T12:00:12.000Z",
        }),
      ),
      opportunityMonitorRow(
        watch({
          opportunity_id: "below",
          current_net_edge: -0.002,
          last_scanned_at: "2026-09-15T12:00:45.000Z",
        }),
      ),
    ];
    const sorted = sortOpportunityMonitor(rows, null);
    assert.deepEqual(
      sorted.map((row) => row.id),
      ["qual-high", "qual-low", "near-new", "near-old", "below", "stale"],
    );
    assert.equal(compareDefaultOpportunityOrder(rows[4], rows[3]) < 0, true);
  });

  it("applies user sorting to the loaded set with nulls last in both directions", () => {
    const rows = [
      opportunityMonitorRow(watch({ opportunity_id: "null-net", current_net_edge: null })),
      opportunityMonitorRow(watch({ opportunity_id: "low", current_net_edge: 0.002 })),
      opportunityMonitorRow(watch({ opportunity_id: "high", current_net_edge: 0.02 })),
    ];
    const desc = sortOpportunityMonitor(rows, { column: "netEdge", direction: "desc" });
    const asc = sortOpportunityMonitor(rows, { column: "netEdge", direction: "asc" });
    assert.deepEqual(
      desc.map((row) => row.id),
      ["high", "low", "null-net"],
    );
    assert.deepEqual(
      asc.map((row) => row.id),
      ["low", "high", "null-net"],
    );
    assert.equal(initialOpportunityMonitorDirection("netEdge"), "desc");
    assert.equal(initialOpportunityMonitorDirection("event"), "asc");
    const first = nextOpportunityMonitorSort(null, "age");
    assert.deepEqual(first, { column: "age", direction: "desc" });
    assert.deepEqual(nextOpportunityMonitorSort(first, "age"), { column: "age", direction: "asc" });
  });

  it("sorts mapping with unavailable values last in both directions", () => {
    const rows = [
      opportunityMonitorRow(watch({ opportunity_id: "missing-map" })),
      opportunityMonitorRow(
        watch({
          opportunity_id: "low-map",
          mapping_confidence: 0.9,
          mapping_provenance: { mapping_source: "native_deterministic" },
        }),
      ),
      opportunityMonitorRow(
        watch({
          opportunity_id: "high-map",
          mapping_confidence: 1,
          mapping_provenance: { mapping_source: "native_deterministic" },
        }),
      ),
    ];
    assert.equal(rows[0].mappingText, "—");
    const desc = sortOpportunityMonitor(rows, { column: "mapping", direction: "desc" });
    const asc = sortOpportunityMonitor(rows, { column: "mapping", direction: "asc" });
    assert.equal(desc[desc.length - 1].id, "missing-map");
    assert.equal(asc[asc.length - 1].id, "missing-map");
    assert.deepEqual(
      desc.map((row) => row.id),
      ["high-map", "low-map", "missing-map"],
    );
    assert.deepEqual(
      asc.map((row) => row.id),
      ["low-map", "high-map", "missing-map"],
    );
  });
});

describe("opportunity monitor age, provenance, navigation, legs, empty honesty", () => {
  it("ages from last_scanned_at with exact timestamp, not browser receipt", () => {
    const item = watch({
      opportunity_id: "age",
      last_scanned_at: "2026-09-15T12:00:00.000Z",
      last_seen_at: "2026-09-15T12:05:00.000Z",
    });
    assert.equal(opportunityObservationTimestamp(item), "2026-09-15T12:00:00.000Z");
    const origin = Date.parse("2026-09-15T12:00:00.000Z");
    assert.equal(formatObservationAge(item.last_scanned_at, origin + 12_000), "12s");
    assert.equal(formatObservationAge(item.last_scanned_at, origin + 48_000), "48s");
    assert.equal(formatObservationAge(item.last_scanned_at, origin + 60_000), "1m");
    assert.equal(formatObservationAge(item.last_scanned_at, origin + 180_000), "3m");
    assert.equal(OBSERVATION_AGE_TICK_MS, 1000);
  });

  it("labels HOT as Fast Scan and UNIVERSE as Full Sweep", () => {
    assert.equal(scanLaneLabel("hot"), "Fast Scan / HOT");
    assert.equal(scanLaneLabel("universe"), "Full Sweep / UNIVERSE");
    assert.equal(scanLaneLabel(null), "—");
    const hot = opportunityMonitorRow(watch({ opportunity_id: "hot", scan_lane: "hot", freshness_class: "executable" }));
    const universe = opportunityMonitorRow(
      watch({ opportunity_id: "uni", scan_lane: "universe", freshness_class: "radar_current" }),
    );
    assert.equal(hot.laneLabel, "Fast Scan / HOT");
    assert.equal(hot.freshnessLabel, "executable");
    assert.equal(universe.laneLabel, "Full Sweep / UNIVERSE");
    assert.equal(universe.freshnessLabel, "radar current");
  });

  it("navigates through the canonical fixture detail seam", () => {
    const row = opportunityMonitorRow(watch({ opportunity_id: "nav" }));
    assert.equal(row.href, trackedMarketHref("evt/leeds-newcastle"));
    assert.equal(row.href, fixtureDetailHref("evt/leeds-newcastle"));
    assert.equal(shouldNavigateFromRowClick({}), true);
    assert.equal(shouldNavigateFromRowClick({ selectedText: "Leeds" }), false);
  });

  it("expands multi-outcome legs from the watchlist read model without inventing action", () => {
    const row = opportunityMonitorRow(watch({ opportunity_id: "legs" }));
    const lines = opportunityLegViews(watch({ opportunity_id: "legs" })).map(formatOpportunityLegLine);
    assert.equal(row.legs.length, 3);
    assert.match(lines[0], /home — matchbook @ 2.1/);
    assert.match(lines[1], /draw — matchbook @ 3.4/);
    assert.match(lines[2], /away — kalshi @ 4.2/);
    assert.match(lines[0], /action —/);
    assert.match(lines[0], /depth/);
    assert.match(lines[0], /source mb-1/);
    assert.match(lines[0], /stake —/);
    assert.equal(mappingDisplay().text, "—");
    assert.equal(mappingDisplay().title, MAPPING_UNAVAILABLE_TITLE);
    assert.equal(row.mappingText, "—");
    assert.equal(row.offerVerify, false);
  });

  it("renders a 2-way selected-solver pair with exact source ids and unknown action", () => {
    const twoWay = watch({
      opportunity_id: "two-way",
      legs: [
        {
          outcome: "yes",
          venue: "matchbook",
          source_market_id: "mb-yes",
          source_runner_id: "y",
          currency: "GBP",
          gbp_stake: 40,
          net_decimal_odds: 2.2,
          cumulative_depth_gbp: 40,
        },
        {
          outcome: "no",
          venue: "polymarket",
          source_market_id: "pm-no",
          source_runner_id: "n",
          currency: "USD",
          gbp_stake: 60,
          net_decimal_odds: 1.9,
          cumulative_depth_gbp: 60,
        },
      ],
    });
    const lines = opportunityLegViews(twoWay).map(formatOpportunityLegLine);
    assert.equal(lines.length, 2);
    assert.match(lines[0], /yes — matchbook @ 2.2/);
    assert.match(lines[1], /no — polymarket @ 1.9/);
    assert.match(lines[0], /action —/);
    assert.match(lines[1], /action —/);
    assert.match(lines[0], /source mb-yes/);
    assert.match(lines[1], /source pm-no/);
    assert.match(lines[0], /stake/);
  });

  it("shows current mapping confidence and Verify only with a safe current candidate", () => {
    const candidate = {
      sides: [
        {
          venue: "matchbook" as const,
          source_event_id: "mb-leeds",
          source_market_id: "mb-mkt",
          raw_home_team: "Leeds United",
          raw_away_team: "Chelsea",
          raw_competition: "Premier League",
          kickoff_utc: "2026-09-20T15:00:00.000Z",
        },
        {
          venue: "polymarket" as const,
          source_event_id: "pm-leeds",
          source_market_id: "pm-mkt",
          raw_home_team: "Leeds United FC",
          raw_away_team: "Chelsea FC",
          raw_competition: "Premier League",
          kickoff_utc: "2026-09-20T15:00:00.000Z",
        },
      ],
      current_confidence: 0.96,
      current_reasons: ["home_team_fuzzy"],
      current_matched: true,
    };
    const below = opportunityMonitorRow(
      watch({
        opportunity_id: "map-below",
        mapping_confidence: 0.96,
        mapping_reasons: ["home_team_fuzzy"],
        mapping_provenance: { mapping_source: "native_deterministic" },
        mapping_review_candidate: candidate,
      }),
    );
    assert.match(below.mappingText, /96\.0%/);
    assert.equal(below.offerVerify, true);
    assert.ok(below.mappingCandidate);

    const native = opportunityMonitorRow(
      watch({
        opportunity_id: "map-native",
        mapping_confidence: 1,
        mapping_provenance: { mapping_source: "native_deterministic" },
        mapping_review_candidate: candidate,
      }),
    );
    assert.match(native.mappingText, /100\.0%/);
    assert.match(native.mappingText, /native deterministic/);
    assert.equal(native.offerVerify, false);

    const learned = opportunityMonitorRow(
      watch({
        opportunity_id: "map-learned",
        mapping_confidence: 1,
        mapping_provenance: {
          mapping_source: "operator_verified",
          rule_id: "maprule:abc",
          rule_version: 2,
        },
        mapping_review_candidate: candidate,
      }),
    );
    assert.match(learned.mappingText, /operator_verified \/ learned alias maprule:abc v2/);
    assert.equal(learned.offerVerify, false);

    const missingCandidate = opportunityMonitorRow(
      watch({
        opportunity_id: "map-no-candidate",
        mapping_confidence: 0.94,
        mapping_provenance: { mapping_source: "native_deterministic" },
      }),
    );
    assert.match(missingCandidate.mappingText, /94\.0%/);
    assert.equal(missingCandidate.offerVerify, false);
  });

  it("does not let richer mapping metadata turn a radar-current trigger into QUALIFYING", () => {
    const aged = watch({
      opportunity_id: "aged-mapped",
      status: "TRIGGERED",
      classification: "triggered_opportunity",
      is_arbitrage: true,
      freshness_class: "radar_current",
      bet_actionable: false,
      mapping_confidence: 0.96,
      mapping_review_candidate: {
        sides: [
          {
            venue: "matchbook",
            source_event_id: "a",
            source_market_id: "m1",
            raw_home_team: "Leeds",
            raw_away_team: "Newcastle",
            raw_competition: "PL",
            kickoff_utc: "2026-09-20T15:00:00Z",
          },
          {
            venue: "kalshi",
            source_event_id: "k",
            source_market_id: "m2",
            raw_home_team: "Leeds United",
            raw_away_team: "Newcastle",
            raw_competition: "PL",
            kickoff_utc: "2026-09-20T15:00:00Z",
          },
        ],
      },
    });
    assert.equal(opportunityMonitorState(aged), "STALE");
    assert.notEqual(opportunityMonitorState(aged), "QUALIFYING");
    assert.equal(opportunityMonitorRow(aged).offerVerify, true);
  });

  it("keeps empty current radar empty and uses live-refresh venues when present", () => {
    const empty = opportunityMonitorSummary([], refresh(), true, true, Date.parse("2026-09-15T12:00:12.000Z"));
    assert.equal(empty.qualifyingCount, 0);
    assert.equal(empty.nearCount, 0);
    assert.equal(empty.bestNetEdge, null);
    assert.equal(empty.newestObservedAt, null);
    assert.equal(empty.dataClass, "LIVE PAPER");
    assert.match(empty.activeVenues, /matchbook/);
    assert.match(empty.fastScan, /Fast scan/);
    assert.match(empty.fullSweep, /Full sweep/);
    const unavailable = opportunityMonitorSummary([], null, false, false);
    assert.equal(unavailable.qualifyingCount, null);
    assert.equal(unavailable.bestNetEdge, null);
    assert.equal(unavailable.dataClass, "UNAVAILABLE");
    assert.equal(unavailable.activeVenues, "—");
  });
});

describe("opportunity monitor table contract", () => {
  const table = readFileSync(join(frontendRoot, "components/opportunity-monitor.tsx"), "utf8");
  const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
  const trades = readFileSync(join(frontendRoot, "components/paper-trade-book.tsx"), "utf8");
  const display = readFileSync(join(frontendRoot, "lib/paper-position-management-display.ts"), "utf8");

  it("uses one shared observation-age timer and accessible sort/navigation", () => {
    assert.match(table, /startSharedObservationAgeTimer\(setNowMs/);
    assert.match(table, /formatObservationAge\(row\.observedAt, nowMs\)/);
    assert.match(table, /observationTimestampTitle\(row\.observedAt\)/);
    assert.equal([...table.matchAll(/setInterval/g)].length, 0);
    assert.match(table, /type="button"/);
    assert.match(table, /aria-sort=\{ariaSort\}/);
    assert.match(table, /fixture-link tracked-market-link/);
    assert.match(table, /Toggle outcome legs/);
    assert.match(table, /loaded current set only/);
    assert.match(table, /MappingVerificationPanel/);
    assert.match(table, /opportunity-mapping-verify/);
    assert.doesNotMatch(table, /tabIndex=\{0\}/);
  });

  it("preserves Open paper positions management copy and does not absorb trades", () => {
    assert.match(page, /Open paper positions/);
    assert.match(page, /<PaperTradeBook/);
    assert.match(trades, /Active trades/);
    assert.match(trades, /formatPositionManagementCell/);
    assert.match(display, /PENDING CONFIRMATION/);
    assert.match(display, /UNWIND ELIGIBLE/);
    assert.match(display, /NOT SAFE/);
    assert.match(display, /after authoritative settlement/);
    assert.doesNotMatch(table, /UNWIND ELIGIBLE/);
    assert.doesNotMatch(table, /close-now/);
    assert.doesNotMatch(page, /MappingVerificationPanel/);
  });

  it("remains presentation-only: no venue write, simulate-fill, unwind, or settle path", () => {
    // Near-only / non-arb radar rows stay display-only; paper capture is server persist, not this table.
    assert.match(table, /Paper describes execution mode, not this table/);
    assert.match(table, /Current radar only/);
    for (const banned of [
      "place_order",
      "cancel_order",
      "sign_order",
      "submit_order",
      "simulate-fill",
      "/unwind",
      "/settle",
      "geobypass",
    ]) {
      assert.doesNotMatch(table, new RegExp(banned));
    }
  });
});
