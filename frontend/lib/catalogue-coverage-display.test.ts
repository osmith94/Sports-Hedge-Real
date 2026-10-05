import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { DiscoveredFixture, LiveRefreshStatus } from "./api";
import {
  coverageRowLabel,
  universeArchetypeSummaryLines,
} from "./catalogue-coverage-display";

function fixture(overrides: Partial<DiscoveredFixture> = {}): DiscoveredFixture {
  return {
    source: "matchbook",
    source_event_id: "mb-1",
    canonical_event_id: "evt/example",
    home_team: "Brentford",
    away_team: "Chelsea",
    competition: "Premier League",
    kickoff_utc: "2026-09-18T19:00:00Z",
    polymarket_matched: false,
    kalshi_matched: true,
    matchbook_matched: true,
    live_score_supported: false,
    last_seen_at: "2026-09-18T12:00:00Z",
    matched_market_count: 4,
    matched_equivalent_count: 4,
    solver_is_arbitrage: false,
    catalogue_coverage: {
      venue_pair: "matchbook_kalshi",
      approved_equivalent: 3,
      paper_assumed_equivalent: 1,
      review_required: 0,
      venue_unavailable: 6,
      rows: [
        {
          archetype: "match_result_1x2",
          display_label: "1X2",
          state: "paper_assumed_equivalent",
          reason: "paper_assumed_equivalent",
        },
        {
          archetype: "both_teams_to_score",
          display_label: "BTTS",
          state: "approved_equivalent",
          reason: "approved_equivalent",
        },
        {
          archetype: "total_goals_half_line",
          display_label: "TOTAL 2.5",
          state: "approved_equivalent",
          reason: "approved_equivalent",
        },
        {
          archetype: "first_team_to_score",
          display_label: "FTTS",
          state: "approved_equivalent",
          reason: "approved_equivalent",
        },
        {
          archetype: "draw_no_bet",
          display_label: "DNB",
          state: "venue_unavailable",
          reason: "Kalshi does not offer Draw No Bet",
        },
      ],
    },
    ...overrides,
  };
}

function status(overrides: Partial<LiveRefreshStatus> = {}): LiveRefreshStatus {
  return {
    discovery_source: "matchbook",
    matching_venue: "kalshi",
    server_loop_enabled: true,
    interval_seconds: 30,
    cycle_in_progress: false,
    live_scores: "unavailable_unless_matchbook_payload_includes_scores",
    discovered_fixtures: [fixture()],
    ...overrides,
  };
}

describe("catalogue coverage display", () => {
  it("formats per-archetype operator lines including paper-assumed 1X2", () => {
    const rows = fixture().catalogue_coverage?.rows ?? [];
    assert.equal(
      coverageRowLabel(rows[0]),
      "1X2             REGISTERED_EQUIVALENT — paper assumed equivalent",
    );
    assert.match(coverageRowLabel(rows[1]), /BTTS\s+APPROVED_EQUIVALENT/);
    assert.match(coverageRowLabel(rows[4]), /DNB\s+VENUE_UNAVAILABLE/);
  });

  it("summarizes UNIVERSE coverage by archetype not only a total", () => {
    const live = status({
      universe: {
        cadence_seconds: 120,
        last_diagnostics: {
          matching_coverage: {
            equivalent_markets: 4,
            catalogue_by_archetype: {
              match_result_1x2: { paper_assumed_equivalent: 1 },
              both_teams_to_score: { approved_equivalent: 1 },
            },
          },
        },
      },
    });
    const lines = universeArchetypeSummaryLines(live);
    assert.ok(lines.some((line) => line.startsWith("match_result_1x2:")));
    assert.ok(lines.some((line) => line.includes("1 registered equivalent")));
    assert.ok(lines.some((line) => line.startsWith("both_teams_to_score:")));
    assert.ok(lines.some((line) => line.includes("1 approved")));
  });
});
