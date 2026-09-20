import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { PaperTrade, PaperTradeLeg } from "./api";
import { money } from "./format";
import {
  compactFillKindLabel,
  compactLegLine,
  compactLegLines,
  compactMarketHeading,
  formatCompactDecimalOdds,
  formatStoredLine,
} from "./paper-trade-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function leg(overrides: Partial<PaperTradeLeg> = {}): PaperTradeLeg {
  return {
    venue: "matchbook",
    outcome: "over",
    currency: "GBP",
    requested_stake: "0.44",
    filled_stake: "0.44",
    displayed_odds: "1.935",
    filled_odds: "1.935",
    source_market_id: "mb-total-2-5",
    fill_kind: "INTERNAL_SIMULATED",
    capital_source: "AUTO_POOL",
    execution_mode: "INTERNAL",
    ...overrides,
  };
}

function trade(overrides: Partial<PaperTrade> = {}): PaperTrade {
  return {
    trade_id: "ptrade-total",
    opportunity_id: "opp-total",
    market_family: "total_goals",
    market_label: "total goals",
    line: "2.5",
    fixture_label: "Arsenal v Chelsea",
    home_team: "Arsenal",
    away_team: "Chelsea",
    state: "OPEN",
    opened_at: "2026-09-20T17:00:00.000Z",
    last_updated_at: "2026-09-20T17:00:00.000Z",
    capital_locked_native: { GBP: "0.44", USD: "0.52" },
    provenance: "live_paper",
    paper_only: true,
    places_orders: false,
    legs: [
      leg(),
      leg({
        venue: "kalshi",
        outcome: "under",
        currency: "USD",
        requested_stake: "0.52",
        filled_stake: "0.52",
        displayed_odds: "2.168",
        filled_odds: "2.168",
        source_market_id: "kx-total-2-5",
      }),
    ],
    audit: [],
    ...overrides,
  };
}

describe("active-trade canonical line and compact odds", () => {
  it("renders Total Goals 2.5 and the two actual bets from stored line, not labels", () => {
    const row = trade();
    assert.equal(compactMarketHeading(row), "Total Goals 2.5");
    assert.equal(
      compactLegLine(row, row.legs[0]),
      `Matchbook · OVER 2.5 @ 1.935 · ${money("0.44")} · PAPER`,
    );
    assert.equal(
      compactLegLine(row, row.legs[1]),
      `Kalshi · UNDER 2.5 @ 2.168 · ${money("0.52", "USD")} · PAPER`,
    );
    const lines = compactLegLines(row);
    assert.equal(lines.length, 2);
    assert.match(lines[0], /Matchbook · OVER 2\.5 @ 1\.935/);
    assert.match(lines[1], /Kalshi · UNDER 2\.5 @ 2\.168/);
  });

  it("shows n.a. for historical line-based trades without a stored line and never guesses from labels or odds", () => {
    const row = trade({
      line: null,
      market_label: "Total Goals 2.5 Over/Under",
      legs: [
        leg({ filled_odds: "1.935", displayed_odds: "1.935" }),
        leg({
          venue: "kalshi",
          outcome: "under",
          currency: "USD",
          filled_odds: "2.168",
          displayed_odds: "2.168",
        }),
      ],
    });
    assert.equal(compactMarketHeading(row), "Total Goals n.a.");
    assert.match(compactLegLine(row, row.legs[0]), /OVER n\.a\. @ 1\.935/);
    assert.equal(formatStoredLine(row.market_label), null);
    assert.doesNotMatch(compactMarketHeading(row), /2\.5/);
  });

  it("formats compact decimal odds to exactly 3dp without mutating stored values", () => {
    const row = trade({
      legs: [
        leg({ filled_odds: "1.9351", displayed_odds: "1.9351" }),
        leg({
          venue: "kalshi",
          outcome: "under",
          currency: "USD",
          filled_odds: "2.1",
          displayed_odds: "2.1",
        }),
      ],
    });
    assert.equal(formatCompactDecimalOdds("1.935"), "1.935");
    assert.equal(formatCompactDecimalOdds("2.1"), "2.100");
    assert.equal(formatCompactDecimalOdds("2.168"), "2.168");
    assert.equal(formatCompactDecimalOdds(null), "—");
    assert.match(compactLegLine(row, row.legs[0]), /@ 1\.935 ·/);
    assert.match(compactLegLine(row, row.legs[1]), /@ 2\.100 ·/);
    assert.equal(row.legs[0].filled_odds, "1.9351");
    assert.equal(row.legs[1].filled_odds, "2.1");
  });

  it("keeps compact Active Trades formatting display-only; detail retains raw stored odds", () => {
    const book = readFileSync(join(frontendRoot, "components/paper-trade-book.tsx"), "utf8");
    assert.match(book, /compactMarketHeading/);
    assert.match(book, /compactLegLines/);
    assert.match(book, /LegsCell/);
    assert.doesNotMatch(book, /legsLine/);
    assert.doesNotMatch(book, /trade\.market_label \?\? trade\.market_family/);

    const detail = readFileSync(join(frontendRoot, "app/paper/[tradeId]/page.tsx"), "utf8");
    assert.match(detail, /leg\.filled_odds \?\? leg\.displayed_odds/);
    assert.doesNotMatch(detail, /formatCompactDecimalOdds/);
    assert.doesNotMatch(detail, /toFixed\(3\)/);

    const formatter = readFileSync(join(frontendRoot, "lib/paper-trade-display.ts"), "utf8");
    assert.match(formatter, /toFixed\(3\)/);
    assert.match(formatter, /formatStoredLine\(trade\.line\)/);
    assert.doesNotMatch(formatter, /parse.*market_label/);
    assert.doesNotMatch(formatter, /filled_odds.*line/);
    const row = trade();
    assert.equal(compactFillKindLabel("INTERNAL_SIMULATED"), "PAPER");
    assert.equal(compactFillKindLabel("PAPER_SIMULATED_EXTERNAL"), "SIMULATED");
    assert.equal(compactFillKindLabel("MANUAL_EXTERNAL"), "MANUAL");
    assert.equal(compactFillKindLabel("UNFILLED"), null);
    assert.doesNotMatch(compactLegLine(row, row.legs[0]), /INTERNAL_SIMULATED/);
    assert.match(compactLegLine(row, row.legs[0]), /PAPER$/);
    assert.doesNotMatch(book, /INTERNAL_SIMULATED/);
    assert.match(book, /paper-trade-fixture-name/);
    assert.match(book, /paper-trade-market/);
    assert.match(book, /paper-trade-actions/);
    assert.match(detail, /leg\.fill_kind/);
    assert.match(book, /leg\.fill_kind/);
  });
});
