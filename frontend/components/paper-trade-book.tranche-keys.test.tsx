import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PaperTrade, PaperTradeLeg } from "../lib/api";
import { ManualSettlementLegList, PaperTradeBook } from "./paper-trade-book";

function leg(overrides: Partial<PaperTradeLeg> = {}): PaperTradeLeg {
  return {
    venue: "matchbook",
    outcome: "yes",
    currency: "GBP",
    requested_stake: "10",
    filled_stake: "10",
    displayed_odds: "2.0",
    filled_odds: "2.0",
    source_market_id: "mb-btts",
    fill_id: "fill-mb-yes",
    tranche_id: "opening",
    fill_kind: "INTERNAL_SIMULATED",
    capital_source: "AUTO_POOL",
    execution_mode: "INTERNAL",
    ...overrides,
  };
}

function trade(): PaperTrade {
  return {
    trade_id: "ptrade-tranches",
    opportunity_id: "opp-tranches",
    market_family: "both_teams_to_score",
    fixture_label: "Newcastle v Arsenal",
    home_team: "Newcastle",
    away_team: "Arsenal",
    state: "OPEN",
    opened_at: "2026-09-20T17:00:00.000Z",
    last_updated_at: "2026-09-20T17:00:00.000Z",
    capital_locked_native: { GBP: "20", USD: "20" },
    provenance: "live_paper",
    paper_only: true,
    places_orders: false,
    legs: [
      leg({ fill_id: "mb-yes-1", tranche_id: "opening" }),
      leg({ fill_id: "mb-yes-2", tranche_id: "topup-1" }),
      leg({
        venue: "kalshi",
        outcome: "no",
        currency: "USD",
        source_market_id: "kx-btts",
        fill_id: "ks-no-1",
        tranche_id: "opening",
      }),
      leg({
        venue: "kalshi",
        outcome: "no",
        currency: "USD",
        source_market_id: "kx-btts",
        fill_id: "ks-no-2",
        tranche_id: "topup-1",
      }),
    ],
    audit: [],
  };
}

function withConsoleErrors(render: () => string): { html: string; errors: string[] } {
  const errors: string[] = [];
  const original = console.error;
  console.error = (...args: unknown[]) => {
    errors.push(args.map((item) => String(item)).join(" "));
  };
  try {
    return { html: render(), errors };
  } finally {
    console.error = original;
  }
}

describe("iterative paper tranche keys", () => {
  it("renders repeated Matchbook YES and Kalshi NO top-ups without duplicate-key warnings", () => {
    const row = trade();
    const { html, errors } = withConsoleErrors(() =>
      renderToStaticMarkup(
        createElement(PaperTradeBook, {
          summary: null,
          active: [row],
          closed: [],
          apiAvailable: true,
          compact: true,
        }),
      ),
    );
    const duplicate = errors.filter((line) => /same key/i.test(line));
    expect(duplicate).toEqual([]);
    expect(html.match(/class="paper-trade-leg"/g)?.length).toBe(4);
    expect(html).toContain("Settle completed trade");
  });

  it("renders manual settlement legs when venue and outcome repeat", () => {
    const legs = trade().legs;
    const { html, errors } = withConsoleErrors(() =>
      renderToStaticMarkup(createElement(ManualSettlementLegList, { legs })),
    );
    const duplicate = errors.filter((line) => /same key/i.test(line));
    expect(duplicate).toEqual([]);
    expect(html.match(/<li/g)?.length).toBe(4);
    expect(html.match(/kalshi · no/g)?.length).toBe(2);
    expect(html.match(/matchbook · yes/g)?.length).toBe(2);
  });
});
