import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { ArbitrageOpportunity } from "./arbitrage-ops";
import { fixtureDetailHref, fixtureHref } from "./discovered-fixture-display";
import {
  ariaSortForColumn,
  nextTrackedMarketSort,
  shouldNavigateFromRowClick,
  sortIndicator,
  sortTrackedMarkets,
  trackedMarketHref,
} from "./tracked-markets-display";
import { opportunityFromWatchlist } from "./watchlist";
import { NearOpportunity } from "./api";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function opportunity(
  overrides: Partial<ArbitrageOpportunity> & Pick<ArbitrageOpportunity, "id" | "eventLabel">,
): ArbitrageOpportunity {
  return {
    provenance: "LIVE_PAPER",
    marketLabel: "Match result",
    settlement: "regulation time",
    venues: ["matchbook"],
    netArb: 0.008,
    grossArb: 0.009,
    trigger: 0.01,
    distanceToTriggerPp: 0.2,
    movement: null,
    capitalRequiredGbp: 100,
    expectedLock: null,
    quoteFreshness: null,
    executableDepth: null,
    limitingLeg: null,
    riskFlags: [],
    currencies: ["GBP"],
    status: "WATCHING",
    executable: false,
    scannedAt: "2026-09-13T12:00:00Z",
    guaranteedProfitGbp: null,
    ...overrides,
  };
}

function watchlistItem(overrides: Partial<NearOpportunity> = {}): NearOpportunity {
  return {
    opportunity_id: "opp-1",
    canonical_event_id: "evt/man-city-arsenal",
    canonical_market_id: "mkt-1",
    home_team: "Manchester City",
    away_team: "Arsenal",
    competition: "Premier League",
    market_family: "match_result",
    period: "full_time",
    venues: ["matchbook", "polymarket"],
    legs: [{ outcome: "home", venue: "matchbook", source_market_id: "mb-1", currency: "GBP" }],
    status: "WATCHING",
    classification: "watch_candidate",
    trigger_net_edge: 0.01,
    current_net_edge: 0.008,
    gross_edge: 0.009,
    distance_to_trigger_pp: 0.2,
    first_seen_at: "2026-09-13T12:00:00Z",
    last_seen_at: "2026-09-13T12:01:00Z",
    rejection_reasons: [],
    insufficiency_reasons: [],
    ...overrides,
  };
}

describe("tracked market sort", () => {
  it("toggles the active header between ascending and descending", () => {
    const first = nextTrackedMarketSort(null, "netMargin");
    assert.deepEqual(first, { column: "netMargin", direction: "asc" });
    const second = nextTrackedMarketSort(first, "netMargin");
    assert.deepEqual(second, { column: "netMargin", direction: "desc" });
    const switched = nextTrackedMarketSort(second, "status");
    assert.deepEqual(switched, { column: "status", direction: "asc" });
  });

  it("exposes aria-sort for the active column only", () => {
    const sort = { column: "grossEdge" as const, direction: "desc" as const };
    assert.equal(ariaSortForColumn("grossEdge", sort), "descending");
    assert.equal(ariaSortForColumn("status", sort), "none");
    assert.equal(sortIndicator("grossEdge", sort), "▼");
    assert.equal(sortIndicator("status", sort), "");
  });

  it("sorts numeric columns numerically, not lexicographically", () => {
    const rows = [
      opportunity({ id: "ten", eventLabel: "Ten", distanceToTriggerPp: 10, netArb: 0.01 }),
      opportunity({ id: "nine", eventLabel: "Nine", distanceToTriggerPp: 9, netArb: 0.09 }),
    ];
    const ascending = sortTrackedMarkets(
      rows,
      { column: "distanceToBackendTrigger", direction: "asc" },
      0.01,
    );
    assert.deepEqual(
      ascending.map((row) => row.id),
      ["nine", "ten"],
    );
  });

  it("keeps null and unknown numeric values at the bottom in both directions", () => {
    const rows = [
      opportunity({ id: "null-net", eventLabel: "Null", netArb: null, grossArb: null }),
      opportunity({ id: "low", eventLabel: "Low", netArb: 0.002, grossArb: 0.003 }),
      opportunity({ id: "high", eventLabel: "High", netArb: 0.02, grossArb: 0.03 }),
    ];
    const asc = sortTrackedMarkets(rows, { column: "netMargin", direction: "asc" }, 0.01);
    const desc = sortTrackedMarkets(rows, { column: "netMargin", direction: "desc" }, 0.01);
    assert.deepEqual(
      asc.map((row) => row.id),
      ["low", "high", "null-net"],
    );
    assert.deepEqual(
      desc.map((row) => row.id),
      ["high", "low", "null-net"],
    );
  });

  it("sorts source/last updated by timestamp, with missing timestamps last", () => {
    const rows = [
      opportunity({ id: "unknown", eventLabel: "Unknown", scannedAt: null }),
      opportunity({ id: "older", eventLabel: "Older", scannedAt: "2026-09-13T10:00:00Z" }),
      opportunity({ id: "newer", eventLabel: "Newer", scannedAt: "2026-09-13T12:00:00Z" }),
    ];
    const desc = sortTrackedMarkets(rows, { column: "sourceUpdated", direction: "desc" }, 0.01);
    assert.deepEqual(
      desc.map((row) => row.id),
      ["newer", "older", "unknown"],
    );
  });

    it("does not change economics, status, or classification while reordering", () => {
    const rows = [
      opportunity({ id: "a", eventLabel: "Alpha", status: "REJECTED", netArb: 0.001, executable: false }),
      opportunity({ id: "b", eventLabel: "Beta", status: "TRIGGERED", netArb: 0.012, executable: true }),
    ];
    const sorted = sortTrackedMarkets(rows, { column: "fixture", direction: "desc" }, 0.01);
    assert.equal(sorted[0].id, "b");
    assert.equal(sorted[0].status, "TRIGGERED");
    assert.equal(sorted[0].executable, true);
    assert.equal(sorted[0].netArb, 0.012);
    assert.equal(sorted[1].status, "REJECTED");
    assert.equal(sorted[1].executable, false);
  });

  it("defaults to strongest current opportunity first, with rejected last", () => {
    const rows = [
      opportunity({
        id: "rejected",
        eventLabel: "Rejected",
        status: "REJECTED",
        netArb: 0.04,
        distanceToTriggerPp: 0,
        executable: false,
      }),
      opportunity({
        id: "watch-far",
        eventLabel: "Far",
        status: "WATCHING",
        netArb: 0.001,
        distanceToTriggerPp: 0.9,
        executable: false,
      }),
      opportunity({
        id: "watch-near",
        eventLabel: "Near",
        status: "APPROACHING",
        netArb: 0.009,
        distanceToTriggerPp: 0.1,
        executable: false,
      }),
      opportunity({
        id: "trig-weak",
        eventLabel: "Weak trigger",
        status: "TRIGGERED",
        netArb: 0.011,
        distanceToTriggerPp: 0,
        executable: true,
      }),
      opportunity({
        id: "trig-strong",
        eventLabel: "Strong trigger",
        status: "TRIGGERED",
        netArb: 0.03,
        distanceToTriggerPp: 0,
        executable: true,
      }),
    ];
    const ranked = sortTrackedMarkets(rows, null, 0.01);
    assert.deepEqual(
      ranked.map((row) => row.id),
      ["trig-strong", "trig-weak", "watch-near", "watch-far", "rejected"],
    );
  });

  it("lets a clicked column override the default ranking", () => {
    const rows = [
      opportunity({ id: "trig", eventLabel: "Triggered", status: "TRIGGERED", netArb: 0.02, executable: true }),
      opportunity({ id: "rejected", eventLabel: "Rejected", status: "REJECTED", netArb: 0.001, executable: false }),
    ];
    const byFixture = sortTrackedMarkets(rows, { column: "fixture", direction: "asc" }, 0.01);
    assert.deepEqual(
      byFixture.map((row) => row.id),
      ["rejected", "trig"],
    );
  });
});

describe("tracked market row identity", () => {
  it("propagates canonical_event_id onto the view model", () => {
    const mapped = opportunityFromWatchlist(watchlistItem());
    assert.equal(mapped.canonicalEventId, "evt/man-city-arsenal");
  });

  it("routes rejected and preparable rows to the same fixture detail href as discovery", () => {
    const eventId = "evt/man-city-arsenal";
    const href = trackedMarketHref(eventId);
    assert.equal(href, fixtureDetailHref(eventId));
    assert.equal(href, "/arbitrage/fixtures/evt%2Fman-city-arsenal");
    assert.equal(
      href,
      fixtureHref({
        source: "matchbook",
        source_event_id: "mb-1",
        canonical_event_id: eventId,
        home_team: "Manchester City",
        away_team: "Arsenal",
        competition: "Premier League",
        kickoff_utc: "2026-09-13T15:00:00Z",
        polymarket_matched: true,
        live_score_supported: false,
        last_seen_at: "2026-09-13T12:00:00Z",
        matched_market_count: 1,
        solver_is_arbitrage: false,
      }),
    );
    assert.equal(trackedMarketHref(null), null);
    assert.equal(trackedMarketHref(""), null);
  });

  it("keeps the existing fixture detail route wired to inventory + paper preview", () => {
    const page = readFileSync(
      join(frontendRoot, "app/arbitrage/fixtures/[eventId]/page.tsx"),
      "utf8",
    );
    const inventory = readFileSync(join(frontendRoot, "components/fixture-inventory.tsx"), "utf8");
    const preview = readFileSync(join(frontendRoot, "components/paper-deployment-preview.tsx"), "utf8");
    assert.match(page, /FixtureInventoryWorkspace/);
    assert.match(page, /getFixtureDetail/);
    assert.match(page, /decodeURIComponent\(eventId\)/);
    assert.match(inventory, /PaperDeploymentPreview opportunities=\{detail\.preparable_opportunities/);
    assert.match(preview, /No settlement-equivalent qualified opportunity/);
    assert.match(preview, /Prepare paper legs/);
    assert.match(preview, /Confirm paper OPEN/);
    assert.match(preview, /does not lock treasury or place orders/);
    assert.doesNotMatch(preview, /place_order|cancel_order/);
  });
});

describe("tracked market keyboard and click-through policy", () => {
  it("lets unmodified row clicks navigate, but not text selection or nested controls", () => {
    assert.equal(shouldNavigateFromRowClick({}), true);
    assert.equal(shouldNavigateFromRowClick({ button: 1 }), false);
    assert.equal(shouldNavigateFromRowClick({ metaKey: true }), false);
    assert.equal(shouldNavigateFromRowClick({ selectedText: "Brighton" }), false);
    assert.equal(
      shouldNavigateFromRowClick({
        target: { closest: (selector: string) => (selector.includes("a") ? {} : null) },
      }),
      false,
    );
  });

  it("keeps the fixture link as the keyboard-focusable control", () => {
    const source = readFileSync(join(frontendRoot, "components/tracked-markets.tsx"), "utf8");
    assert.match(source, /<Link[\s\S]*className="fixture-link tracked-market-link"/);
    assert.match(source, /type="button"/);
    assert.match(source, /aria-sort=\{ariaSort\}/);
    assert.match(source, /aria-label=\{`Sort by \$\{label\}/);
    assert.doesNotMatch(source, /tabIndex=\{0\}/);
  });
});
