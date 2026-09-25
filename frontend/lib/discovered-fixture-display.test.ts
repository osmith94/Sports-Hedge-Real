import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { DiscoveredFixture, LiveRefreshStatus } from "./api";
import {
  discoveryCompactCounts,
  discoveryEmptyMatchNote,
  discoveryCompactSummaryLabel,
  discoveryStatusBadgeLabel,
  kickoffClockLabel,
  marketEvaluationLabel,
  opportunityStateLabel,
  viabilityEvidenceSummary,
} from "./discovered-fixture-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function fixture(overrides: Partial<DiscoveredFixture> = {}): DiscoveredFixture {
  return {
    source: "matchbook",
    source_event_id: "mb-1",
    canonical_event_id: "evt/example",
    home_team: "Manchester City",
    away_team: "Arsenal",
    competition: "Premier League",
    kickoff_utc: "2026-09-13T15:00:00Z",
    polymarket_matched: true,
    live_score_supported: false,
    last_seen_at: "2026-09-13T12:00:00Z",
    matched_market_count: 1,
    matched_equivalent_count: 0,
    solver_is_arbitrage: false,
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

describe("fixture discovery compact summary", () => {
  it("uses live discovery counts without inventing fixtures", () => {
    const live = status({
      last_matched_event_pairs: 68,
      skipped_out_of_scope: 200,
      discovered_fixtures: [
        ...Array.from({ length: 97 }, (_, index) =>
          fixture({
            canonical_event_id: `evt/${index}`,
            matchbook_matched: index < 66,
            matched_equivalent_count: 0,
            solver_is_arbitrage: false,
          }),
        ),
        fixture({
          canonical_event_id: "evt/equivalent-a",
          matchbook_matched: true,
          matched_equivalent_count: 5,
          solver_is_arbitrage: false,
        }),
        fixture({
          canonical_event_id: "evt/qualifying",
          matchbook_matched: true,
          matched_equivalent_count: 4,
          solver_is_arbitrage: true,
        }),
      ],
    });

    assert.deepEqual(discoveryCompactCounts(live), {
      fixtures: 99,
      crossVenue: 68,
      equivalent: 9,
      qualifying: 1,
      skipped: 200,
    });
    assert.equal(
      discoveryCompactSummaryLabel(live, true),
      "Fixture Discovery · 99 fixtures · 68 cross-venue · 9 equivalent · 1 qualifying · 200 skipped",
    );
    assert.equal(discoveryStatusBadgeLabel(true, live), "LIVE PAPER · LAST SCAN VENUES UNKNOWN");
    assert.doesNotMatch(discoveryStatusBadgeLabel(true, live), /MB \/ PM \/ K/);
  });

  it("does not fabricate counts when discovery status is unavailable", () => {
    assert.equal(
      discoveryCompactSummaryLabel(null, false),
      "Fixture Discovery · status unavailable",
    );
    assert.equal(discoveryStatusBadgeLabel(false), "DISCOVERY STATUS UNAVAILABLE");
    assert.doesNotMatch(discoveryStatusBadgeLabel(true), /MB \/ PM \/ K/);
    assert.deepEqual(discoveryCompactCounts(null), {
      fixtures: 0,
      crossVenue: 0,
      equivalent: 0,
      qualifying: 0,
      skipped: 0,
    });
  });

  it("treats missing pair/skip totals as zero rather than fabricating coverage", () => {
    const live = status({
      last_matched_event_pairs: null,
      skipped_out_of_scope: null,
      discovered_fixtures: [fixture({ matched_equivalent_count: undefined })],
    });
    assert.deepEqual(discoveryCompactCounts(live), {
      fixtures: 1,
      crossVenue: 0,
      equivalent: 0,
      qualifying: 0,
      skipped: 0,
    });
  });

  it("explains fixture coverage with no shared canonical fixture without inventing a regression", () => {
    const counts = discoveryCompactCounts(
      status({
        last_matched_event_pairs: 53,
        discovered_fixtures: [
          fixture({
            matchbook_matched: false,
            polymarket_matched: true,
            matched_equivalent_count: 0,
          }),
        ],
      }),
    );
    assert.equal(counts.crossVenue, 0);
    assert.match(discoveryEmptyMatchNote(counts) ?? "", /no current multi-venue identity overlap/i);
  });

  it("distinguishes event matches from missing settlement-equivalent markets", () => {
    const counts = discoveryCompactCounts(
      status({
        discovered_fixtures: [
          fixture({
            matchbook_matched: true,
            polymarket_matched: true,
            matched_equivalent_count: 0,
          }),
        ],
      }),
    );
    assert.equal(counts.crossVenue, 1);
    assert.match(discoveryEmptyMatchNote(counts) ?? "", /no settlement-equivalent markets/i);
  });
});

describe("fixture discovery collapsed-by-default disclosure", () => {
  it("renders a collapsed details summary with live badge and show/hide toggle", () => {
    const source = readFileSync(
      join(frontendRoot, "components/fixture-discovery-section.tsx"),
      "utf8",
    );
    assert.match(source, /<details className="discovery-disclosure">/);
    assert.doesNotMatch(source, /\sopen[={]/);
    assert.doesNotMatch(source, /open>/);
    assert.match(source, /discoveryCompactSummaryLabel/);
    assert.match(source, /discoveryStatusBadgeLabel/);
    assert.match(source, /Show discovery/);
    assert.match(source, /Hide discovery/);
    assert.match(source, /discovery-chevron/);
    assert.match(source, /<DiscoveredFixturesPanel status=\{status\} available=\{available\} \/>/);
  });

  it("places HOT pricing fixtures after discovery and keeps Opportunity Monitor as the current-opportunity table", () => {
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const treasuryIndex = page.indexOf("<LiquidityPools");
    const positionsIndex = page.indexOf("<span>Open paper positions</span>");
    const scanIndex = page.indexOf("<RunPaperScan");
    const discoveryIndex = page.indexOf("<FixtureDiscoverySection");
    const hotIndex = page.indexOf("<HotFixturesPanel");
    const monitorIndex = page.indexOf("<OpportunityMonitor");
    const auditIndex = page.indexOf("audit-disclosure");
    assert.ok(treasuryIndex >= 0);
    assert.ok(positionsIndex > treasuryIndex);
    assert.ok(scanIndex > positionsIndex);
    assert.ok(discoveryIndex > scanIndex);
    assert.ok(hotIndex > discoveryIndex);
    assert.ok(monitorIndex > hotIndex);
    assert.ok(auditIndex > monitorIndex);
    assert.doesNotMatch(page, /<span>Tracked<\/span>/);
    assert.doesNotMatch(page, /TrackedMarketsBoard/);
    assert.doesNotMatch(page, /DiscoveredFixturesPanel/);
  });

  it("preserves the expanded warning banner and discovery table in the existing panel", () => {
    const panel = readFileSync(join(frontendRoot, "components/discovered-fixtures.tsx"), "utf8");
    assert.match(panel, /status\.config_warnings/);
    assert.match(panel, /CONFIG_WARNING_BANNER_CLASS/);
    assert.doesNotMatch(panel, /scan-message scan-message-error/);
    assert.match(panel, /table className="discovery-compact"/);
    assert.match(panel, /Discovery status unavailable\. No fabricated fixtures\./);
  });

  it("gates FixtureRow kickoff on hydrated nowMs so SSR and first client render match", () => {
    const panel = readFileSync(join(frontendRoot, "components/discovered-fixtures.tsx"), "utf8");
    const hydrated = readFileSync(join(frontendRoot, "components/hydrated-relative-time.tsx"), "utf8");
    const format = readFileSync(join(frontendRoot, "lib/format.ts"), "utf8");
    assert.match(panel, /useHydratedNowMs/);
    assert.match(panel, /kickoffLocalLabel\(item\.kickoff_utc, nowMs\)/);
    assert.doesNotMatch(panel, /kickoffLocalLabel\(item\.kickoff_utc\)/);
    assert.match(hydrated, /useState<number \| null>\(null\)/);
    assert.match(format, /if \(now == null \|\| !Number\.isFinite\(now\)\) return iso;/);
    assert.notEqual(kickoffClockLabel("2026-09-21T15:00:00.000Z"), "2026-09-21T15:00:00.000Z");
  });

  it("styles the disclosure consistently and keeps visible keyboard focus", () => {
    const css = readFileSync(join(frontendRoot, "app/globals.css"), "utf8");
    assert.match(css, /\.discovery-summary:focus-visible/);
    assert.match(css, /\.discovery-toggle-hide \{ display: none; \}/);
    assert.match(css, /\.discovery-disclosure\[open\] \.discovery-toggle-show \{ display: none; \}/);
    assert.match(css, /\.discovery-disclosure\[open\] \.discovery-toggle-hide \{ display: inline; \}/);
    assert.match(css, /\.discovery-summary::-webkit-details-marker \{ display: none; \}/);
  });
});

describe("single-venue UNIVERSE evaluation honesty", () => {
  it("does not present a cheap single-venue row as evaluated or scan-budget leftover", () => {
    const row = fixture({
      polymarket_matched: false,
      matched_market_count: 0,
      matched_equivalent_count: null,
      market_evaluation_state: "single_venue_no_cross_venue_candidate",
      market_evaluation_reason: "single_venue_no_cross_venue_candidate",
      opportunity_state: "not_evaluated",
    });
    assert.equal(
      marketEvaluationLabel(row),
      "Not evaluated — single-venue (no cross-venue candidate)",
    );
  });

  it("labels a currently-one-viable-venue row as cross-venue unavailable, not finished", () => {
    const row = fixture({
      matchbook_matched: true,
      kalshi_matched: true,
      matched_equivalent_count: null,
      market_evaluation_state: "cross_venue_unavailable",
      market_evaluation_reason: "cross_venue_unavailable",
      opportunity_state: "not_evaluated",
      fixture_status: null,
      in_running: null,
    });
    assert.equal(marketEvaluationLabel(row), "Not evaluated — cross-venue unavailable");
    assert.equal(opportunityStateLabel(row), "not evaluated");
  });

  it("labels a conservative upper-bound prune without calling the fixture finished", () => {
    const row = fixture({
      matchbook_matched: true,
      kalshi_matched: true,
      matched_equivalent_count: null,
      market_evaluation_state: "upper_bound_below_min_net",
      market_evaluation_reason: "upper_bound_below_min_net",
      opportunity_state: "not_evaluated",
      fixture_status: null,
      in_running: null,
    });
    assert.equal(
      marketEvaluationLabel(row),
      "Not evaluated — remaining books cannot reach Min Net Arb",
    );
    assert.equal(opportunityStateLabel(row), "not evaluated");
  });

  it("separates a gone market id from event unavailability", () => {
    const summary = viabilityEvidenceSummary({
      event_viability: {
        matchbook: { state: "viable", evidence_scope: "event", evidence_reason: "event_current" },
        kalshi: { state: "unknown", evidence_scope: "event", evidence_reason: null },
      },
      market_gone: [
        {
          venue: "matchbook",
          native_market_id: "9004",
          evidence_scope: "market",
          evidence_reason: "market_gone",
        },
      ],
      final_reason: "no_comparable_markets",
    });
    assert.match(summary || "", /matchbook viable/);
    assert.match(summary || "", /market gone matchbook 9004 market_gone/);
    assert.doesNotMatch(summary || "", /unavailable/);
  });

  it("shows a closed Kalshi family without marking the fixture terminal", () => {
    const summary = viabilityEvidenceSummary({
      event_viability: {
        matchbook: { state: "viable", evidence_scope: "event", evidence_reason: "event_current" },
        kalshi: { state: "unknown", evidence_scope: "event", evidence_reason: null },
      },
      source_event_viability: [
        {
          venue: "kalshi",
          source_event_id: "KXGAME-NOR",
          state: "terminal",
          evidence_scope: "source_event",
          evidence_reason: "source_event_terminal",
        },
        {
          venue: "kalshi",
          source_event_id: "KXBTTS-NOR",
          state: "viable",
          evidence_scope: "source_event",
          evidence_reason: "source_event_current",
        },
      ],
    });
    assert.match(summary || "", /kalshi unknown/);
    assert.match(summary || "", /source kalshi KXGAME-NOR terminal/);
    assert.match(summary || "", /kalshi KXBTTS-NOR viable/);
    assert.doesNotMatch(summary || "", /event kalshi terminal/);
    assert.doesNotMatch(summary || "", /get_market_timeout/);
  });
});
