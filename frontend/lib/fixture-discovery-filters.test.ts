import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createElement } from "react";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, it } from "vitest";

import type { DiscoveredFixture } from "./api";
import { DiscoveredFixturesPanel } from "../components/discovered-fixtures";
import {
  ALL_DISCOVERY_FILTER,
  applyFixtureDiscoveryFilters,
  competitionFilterOptions,
  sportFilterOptions,
} from "./fixture-discovery-filters";

const panelSource = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "../components/discovered-fixtures.tsx"),
  "utf8",
);
const filterSource = readFileSync(fileURLToPath(import.meta.url).replace(/\.test\.ts$/, ".ts"), "utf8");

function fixture(overrides: Partial<DiscoveredFixture> = {}): DiscoveredFixture {
  return {
    source: "matchbook",
    source_event_id: "mb-1",
    canonical_event_id: "evt/example",
    home_team: "Arsenal",
    away_team: "Chelsea",
    competition: "Premier League",
    sport: "football",
    kickoff_utc: "2026-09-21T15:00:00Z",
    polymarket_matched: true,
    live_score_supported: false,
    last_seen_at: "2026-09-21T12:00:00Z",
    matched_market_count: 1,
    solver_is_arbitrage: false,
    ...overrides,
  };
}

const snapshot = [
  fixture({ canonical_event_id: "evt/pl-1", home_team: "Arsenal", competition: "Premier League", sport: "football" }),
  fixture({ canonical_event_id: "evt/pl-2", home_team: "Liverpool", competition: "Premier League", sport: "football" }),
  fixture({ canonical_event_id: "evt/nfl", home_team: "Chiefs", away_team: "Colts", competition: "NFL", sport: "american_football" }),
  fixture({ canonical_event_id: "evt/mlb", home_team: "Yankees", away_team: "Red Sox", competition: "MLB", sport: "baseball" }),
  fixture({ canonical_event_id: "evt/ufc", home_team: "Fighter A", away_team: "Fighter B", competition: "UFC 310", sport: "mma" }),
  fixture({ canonical_event_id: "evt/boxing", home_team: "Fury", away_team: "Usyk", competition: "Canelo vs Crawford", sport: "boxing" }),
  fixture({ canonical_event_id: "evt/unknown", home_team: "Alpha", away_team: "Beta", competition: "Mystery Card", sport: undefined }),
];

function ids(rows: readonly DiscoveredFixture[]): string[] {
  return rows.map((item) => item.canonical_event_id);
}

describe("fixture discovery sport and competition filters", () => {
  it("shows only sports present in the loaded snapshot with counts", () => {
    const options = sportFilterOptions(snapshot);
    assert.deepEqual(
      options.map((option) => `${option.label} ${option.count}`),
      ["Football 2", "American Football 1", "Baseball 1", "Boxing 1", "MMA 1", "Unknown 1"],
    );
    assert.equal(options.some((option) => option.label === "Tennis"), false);
  });

  it("filters rows by sport without scanner-scope inputs", () => {
    const scope = { selected_competition_codes: ["premier_league"], writes: 0 };
    const filtered = applyFixtureDiscoveryFilters(snapshot, {
      sport: "baseball",
      competition: ALL_DISCOVERY_FILTER,
    });
    assert.deepEqual(ids(filtered), ["evt/mlb"]);
    assert.deepEqual(scope, { selected_competition_codes: ["premier_league"], writes: 0 });
    assert.doesNotMatch(filterSource, /OperatorUniverseScope|universe-scope|collect\(/);
    assert.doesNotMatch(panelSource, /saveUniverseScope|universe-scope|\/paper\/collect/);
    assert.match(panelSource, /applyFixtureDiscoveryFilters/);
    assert.match(panelSource, /useState/);
  });

  it("narrows competition options to the selected sport for leagues and one-off events", () => {
    assert.deepEqual(
      competitionFilterOptions(snapshot, "football").map((option) => option.label),
      ["Premier League"],
    );
    assert.deepEqual(
      competitionFilterOptions(snapshot, "mma").map((option) => `${option.label} ${option.count}`),
      ["UFC 310 1"],
    );
    assert.deepEqual(
      competitionFilterOptions(snapshot, "boxing").map((option) => option.key),
      ["Canelo vs Crawford"],
    );
    const league = applyFixtureDiscoveryFilters(snapshot, {
      sport: "football",
      competition: "Premier League",
    });
    const card = applyFixtureDiscoveryFilters(snapshot, {
      sport: "mma",
      competition: "UFC 310",
    });
    assert.deepEqual(ids(league), ["evt/pl-1", "evt/pl-2"]);
    assert.deepEqual(ids(card), ["evt/ufc"]);
  });

  it("restores the original row set and order for All", () => {
    const sportFiltered = applyFixtureDiscoveryFilters(snapshot, {
      sport: "baseball",
      competition: "MLB",
    });
    assert.deepEqual(ids(sportFiltered), ["evt/mlb"]);
    const restored = applyFixtureDiscoveryFilters(snapshot, {
      sport: ALL_DISCOVERY_FILTER,
      competition: ALL_DISCOVERY_FILTER,
    });
    assert.deepEqual(ids(restored), ids(snapshot));
  });

  it("keeps missing sport visible in the Unknown bucket", () => {
    const unknown = applyFixtureDiscoveryFilters(snapshot, {
      sport: "unknown",
      competition: ALL_DISCOVERY_FILTER,
    });
    assert.deepEqual(ids(unknown), ["evt/unknown"]);
    const all = applyFixtureDiscoveryFilters(snapshot, {
      sport: ALL_DISCOVERY_FILTER,
      competition: ALL_DISCOVERY_FILTER,
    });
    assert.equal(all.some((item) => item.canonical_event_id === "evt/unknown"), true);
  });

  it("renders the default All filter and the original row order", () => {
    const markup = renderToStaticMarkup(
      createElement(DiscoveredFixturesPanel, { fixtures: snapshot, available: true }),
    );
    assert.match(markup, /Sport/);
    assert.match(markup, /Competition \/ Event/);
    assert.match(markup, /All 7/);
    assert.match(markup, /Football 2/);
    assert.match(markup, /American Football 1/);
    assert.match(markup, /Baseball 1/);
    assert.doesNotMatch(markup, /Tennis/);
    const arsenal = markup.indexOf("Arsenal v Chelsea");
    const yankees = markup.indexOf("Yankees v Red Sox");
    const unknown = markup.indexOf("Alpha v Beta");
    assert.ok(arsenal >= 0 && yankees > arsenal && unknown > yankees);
    assert.match(markup, /Display only/);
  });
});
