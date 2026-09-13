import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { DiscoveredFixture, LiveRefreshStatus } from "./api";
import {
  discoveryCompactCounts,
  discoveryCompactSummaryLabel,
  discoveryStatusBadgeLabel,
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
            matched_equivalent_count: 0,
            solver_is_arbitrage: false,
          }),
        ),
        fixture({
          canonical_event_id: "evt/equivalent-a",
          matched_equivalent_count: 5,
          solver_is_arbitrage: false,
        }),
        fixture({
          canonical_event_id: "evt/qualifying",
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
    assert.equal(discoveryStatusBadgeLabel(true), "LIVE PAPER · MB / PM / K");
  });

  it("does not fabricate counts when discovery status is unavailable", () => {
    assert.equal(
      discoveryCompactSummaryLabel(null, false),
      "Fixture Discovery · status unavailable",
    );
    assert.equal(discoveryStatusBadgeLabel(false), "DISCOVERY STATUS UNAVAILABLE");
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

  it("keeps Tracked as the first substantial opportunity table after the compact discovery summary", () => {
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const discoveryIndex = page.indexOf("<FixtureDiscoverySection");
    const trackedIndex = page.indexOf("<span>Tracked</span>");
    const trackedBoardIndex = page.indexOf("<TrackedMarketsBoard");
    assert.ok(discoveryIndex >= 0);
    assert.ok(trackedIndex > discoveryIndex);
    assert.ok(trackedBoardIndex > trackedIndex);
    assert.doesNotMatch(page, /DiscoveredFixturesPanel/);
  });

  it("preserves the expanded warning banner and discovery table in the existing panel", () => {
    const panel = readFileSync(join(frontendRoot, "components/discovered-fixtures.tsx"), "utf8");
    assert.match(panel, /status\.config_warnings/);
    assert.match(panel, /scan-message scan-message-error/);
    assert.match(panel, /table className="discovery-compact"/);
    assert.match(panel, /Discovery status unavailable\. No fabricated fixtures\./);
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
