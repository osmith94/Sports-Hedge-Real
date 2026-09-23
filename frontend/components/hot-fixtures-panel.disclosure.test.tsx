import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createElement } from "react";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, it } from "vitest";

import type { DiscoveredFixture, LiveRefreshStatus } from "../lib/api";
import {
  HOT_REASON_KICKOFF_HORIZON,
  HOT_ROSTER_HEADERS,
  deferredFixtureRows,
} from "../lib/hot-fixture-roster-display";
import { DeferredCrossVenueRoster, HotFixturesPanel } from "./hot-fixtures-panel";

const panelSource = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "hot-fixtures-panel.tsx"), "utf8");

function fixture(overrides: Partial<DiscoveredFixture> = {}): DiscoveredFixture {
  return {
    source: "matchbook",
    source_event_id: "mb-deferred",
    canonical_event_id: "evt/deferred-arsenal",
    home_team: "Arsenal",
    away_team: "Chelsea",
    competition: "Premier League",
    kickoff_utc: "2026-09-16T19:00:00Z",
    polymarket_matched: false,
    live_score_supported: false,
    last_seen_at: "2026-09-16T18:10:00Z",
    matched_market_count: 0,
    solver_is_arbitrage: false,
    scan_lane: "hot",
    market_evaluation_state: "single_venue_no_cross_venue_candidate",
    market_evaluation_reason: "single_venue_no_cross_venue_candidate",
    hot_reasons: [HOT_REASON_KICKOFF_HORIZON],
    ...overrides,
  };
}

function status(discovered: DiscoveredFixture[]): LiveRefreshStatus {
  return {
    discovery_source: "matchbook",
    matching_venue: "polymarket",
    server_loop_enabled: true,
    interval_seconds: 30,
    cycle_in_progress: false,
    live_scores: "unavailable_unless_matchbook_payload_includes_scores",
    discovered_fixtures: discovered,
    hot: { cadence_seconds: 30, fixture_count: 0, evaluated_count: 0 },
    price_engine: { hot: { pricing_fixtures: 0, working_set: 0 } },
    universe: { cadence_seconds: 180, fixture_count: 0 },
  };
}

const rows = deferredFixtureRows(status([fixture()]), null);

function markup(expanded: boolean): string {
  return renderToStaticMarkup(
    createElement(DeferredCrossVenueRoster, {
      rows,
      nowMs: null,
      expanded,
      onToggle: () => undefined,
    }),
  );
}

describe("deferred HOT roster disclosure", () => {
  it("does not render the deferred table until the disclosure state is open", () => {
    const closed = markup(false);
    const open = markup(true);

    assert.match(closed, /DEFERRED \/ AWAITING CROSS-VENUE · 1/);
    assert.match(closed, /aria-expanded="false"/);
    assert.match(closed, /Expand to inspect/);
    assert.doesNotMatch(closed, /<table/);
    assert.doesNotMatch(closed, /hot-fixtures-table/);
    assert.doesNotMatch(closed, /Arsenal v Chelsea/);
    assert.doesNotMatch(closed, /single_venue_no_cross_venue_candidate/);
    assert.doesNotMatch(closed, /<details/);

    assert.match(open, /aria-expanded="true"/);
    assert.match(open, /<table class="hot-fixtures-table"/);
    assert.match(open, /Arsenal v Chelsea/);
    assert.match(open, /Why HOT/);
    assert.match(open, /Evaluation/);
    assert.match(open, /single_venue_no_cross_venue_candidate/);
    for (const header of HOT_ROSTER_HEADERS) {
      assert.match(open, new RegExp(header.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    }
    assert.doesNotMatch(open, /<details/);
  });

  it("keeps the default panel closed so a live-refresh render does not mount the deferred table", () => {
    const html = renderToStaticMarkup(
      createElement(HotFixturesPanel, {
        status: status([fixture(), fixture({ canonical_event_id: "evt/deferred-2", home_team: "Leeds United", away_team: "Newcastle United" })]),
        available: true,
      }),
    );
    assert.match(html, /DEFERRED \/ AWAITING CROSS-VENUE · 2/);
    assert.match(html, /aria-expanded="false"/);
    assert.doesNotMatch(html, /<table/);
    assert.doesNotMatch(html, /Leeds United v Newcastle United/);
    assert.doesNotMatch(html, /<details/);
  });

  it("stores disclosure in component state so refresh rerenders keep it and remount defaults closed", () => {
    const section = panelSource.slice(
      panelSource.indexOf("function DeferredCrossVenueSection"),
      panelSource.indexOf("function FixtureTable"),
    );
    assert.match(section, /useState\(false\)/);
    assert.match(section, /setExpanded\(\(current\) => !current\)/);
    assert.doesNotMatch(section, /useEffect|sessionStorage|localStorage/);
    const tableBranch = panelSource.slice(
      panelSource.indexOf("export function DeferredCrossVenueRoster"),
      panelSource.indexOf("function DeferredCrossVenueSection"),
    );
    assert.match(tableBranch, /\{expanded \? \(/);
    assert.ok(tableBranch.indexOf("<FixtureTable") > tableBranch.indexOf("{expanded ? ("));
    assert.doesNotMatch(panelSource, /<details|<summary/);
  });
});
