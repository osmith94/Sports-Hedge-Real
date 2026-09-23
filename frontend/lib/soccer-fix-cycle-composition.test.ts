import { describe, expect, it } from "vitest";

import { applyCompetitionModalProps } from "./competition-modal-draft";
import type { OpportunityLifecycleEvent, OperatorUniverseScope, PaperTrade } from "./api";
import { compactMarketHeading, formatCompactDecimalOdds } from "./paper-trade-display";
import { activityFromWatchlist } from "./watchlist";

function lifecycle(
  overrides: Partial<OpportunityLifecycleEvent> &
    Pick<OpportunityLifecycleEvent, "event_id" | "event_type">,
): OpportunityLifecycleEvent {
  return {
    opportunity_id: "watch:mkt-total-2-5",
    occurred_at: "2026-09-20T18:00:00.000Z",
    status: "CLOSED",
    fixture_label: "Arsenal v Chelsea",
    market_family: "total_goals",
    canonical_event_id: "evt-compose",
    canonical_market_id: "mkt-total-2-5",
    capture_eligible: true,
    ...overrides,
  };
}

function scope(overrides: Partial<OperatorUniverseScope> = {}): OperatorUniverseScope {
  const selected = overrides.selected_competition_codes ?? ["UCL"];
  return {
    sport: "football",
    selected_competition_codes: selected,
    selected_count: selected.length,
    scope_version: 2,
    registry_version: 3,
    source: "operator",
    needs_first_run_confirmation: false,
    new_competitions_available: false,
    catalog: [],
    ...overrides,
  };
}

describe("soccer fix-cycle composition", () => {
  it("keeps competition-selector draft, exact line/odds, and Trade exited together", () => {
    const preserved = applyCompetitionModalProps(
      { open: true, query: "prem", draft: ["EPL"], saveAsDefault: false },
      { open: true, scope: scope() },
    );
    expect(preserved.draft).toEqual(["EPL"]);
    expect(preserved.query).toBe("prem");

    const trade: PaperTrade = {
      trade_id: "ptrade-compose",
      opportunity_id: "opp-compose",
      market_family: "total_goals",
      line: "2.50",
      state: "CLOSED",
      opened_at: "2026-09-20T17:00:00.000Z",
      last_updated_at: "2026-09-20T18:00:00.000Z",
      capital_locked_native: {},
      provenance: "live_paper",
      paper_only: true,
      places_orders: false,
      legs: [],
      audit: [],
    };
    expect(compactMarketHeading(trade)).toBe("Total Goals 2.5");
    expect(formatCompactDecimalOdds("1.935")).toBe("1.935");

    const cards = activityFromWatchlist([
      lifecycle({ event_id: "closed-1", event_type: "closed", detail: "paper settlement" }),
      lifecycle({
        event_id: "entered-1",
        event_type: "paper_fill_complete",
        status: "FILLED",
        detail: "paper_mode_only",
      }),
      lifecycle({
        event_id: "noise-1",
        event_type: "paper_fill_attempted",
        status: "PAPER_FILLING",
        detail: "should stay hidden",
      }),
    ]);
    expect(cards.map((card) => card.title)).toEqual(["Trade exited", "Trade entered"]);
    expect(cards.every((card) => card.subject === "Arsenal v Chelsea · total goals")).toBe(true);
  });
});
