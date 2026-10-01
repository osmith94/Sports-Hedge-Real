import { describe, expect, it } from "vitest";

import { PreparablePaperOpportunity, Venue, VenueMarketFacts, VenueQuoteFact } from "./api";
import {
  compactQuoteLines,
  compactVenueMeta,
  failingVenueChecks,
  settlementLabel,
} from "./fixture-inventory-display";
import {
  decisionBadgeClass,
  inventoryCardViewModel,
  isPrimaryApprovedFamily,
  operatorDecision,
  paperActionForRow,
  toneClass,
} from "./fixture-inventory-operator";
import type { KalshiFixtureMarketInventoryRow } from "./fixture-inventory-display";

function quote(outcome: string, decimal_odds: number, size_at_touch: number): VenueQuoteFact {
  return { outcome, decimal_odds, size_at_touch };
}

function facts(
  venue: Venue,
  quotes: VenueQuoteFact[],
  extra: Partial<VenueMarketFacts> = {},
): VenueMarketFacts {
  return {
    venue,
    source_event_id: `${venue}-event`,
    source_market_id: `${venue}-mkt`,
    family: extra.family ?? "match_result",
    period: "full_time",
    settlement_complete: true,
    best_backs: quotes,
    native_currency: venue === "matchbook" ? "GBP" : "USD",
    fee_status: "known",
    fee_rate: venue === "matchbook" ? 0.02 : 0,
    fee_label: venue === "matchbook" ? "2.00% net-profit commission" : "Fees disabled",
    fx_status: venue === "matchbook" ? "not_required" : "known",
    usable_depth_at_touch: extra.usable_depth_at_touch ?? quotes[0]?.size_at_touch,
    ...extra,
  };
}

function row(overrides: Partial<KalshiFixtureMarketInventoryRow> = {}): KalshiFixtureMarketInventoryRow {
  return {
    display_name: "Match Result",
    family: "match_result",
    period: "full_time",
    comparison_status: "matched_equivalent",
    rejection_reasons: [],
    match_reasons: [],
    entered_solver: false,
    solver_is_arbitrage: false,
    ...overrides,
  };
}

const homeDrawAway = [
  quote("home", 2.4, 450),
  quote("draw", 3.65, 5542),
  quote("away", 3.15, 645),
];

const yesNo = [
  quote("yes", 2.38, 18389),
  quote("no", 1.69, 9452),
];

const matchResultPartial = row({
  comparison_status: "matched_equivalent",
  entered_solver: true,
  solver_is_arbitrage: false,
  current_net_edge: -0.0194,
  trigger_net_edge: 0.01,
  rejection_reasons: ["incomplete_outcome_set"],
  matchbook: facts("matchbook", homeDrawAway, { usable_depth_at_touch: 450 }),
  polymarket: facts("polymarket", yesNo, { usable_depth_at_touch: 9452 }),
  kalshi: facts("kalshi", homeDrawAway, { usable_depth_at_touch: 450, fee_rate: 0.07 }),
  pair_results: [
    {
      left_venue: "matchbook",
      right_venue: "kalshi",
      entered_solver: true,
      solver_model: "strict_complete_set",
      current_net_edge: -0.0194,
      rejection_reasons: [],
      solver_is_arbitrage: false,
    },
    {
      left_venue: "polymarket",
      right_venue: "kalshi",
      entered_solver: false,
      rejection_reasons: ["incomplete_outcome_set"],
      solver_is_arbitrage: false,
    },
  ],
});

const qualified = row({
  entered_solver: true,
  solver_is_arbitrage: true,
  solver_model: "strict_complete_set",
  current_net_edge: 0.012,
  trigger_net_edge: 0.01,
  distance_to_trigger_pp: -0.2,
  matchbook: facts("matchbook", homeDrawAway),
  kalshi: facts("kalshi", homeDrawAway),
  pair_results: [
    {
      left_venue: "matchbook",
      right_venue: "kalshi",
      entered_solver: true,
      solver_model: "strict_complete_set",
      current_net_edge: 0.012,
      rejection_reasons: [],
      solver_is_arbitrage: true,
    },
  ],
});

const preparable: PreparablePaperOpportunity[] = [
  {
    opportunity_id: "opp-1",
    solver_model: "strict_complete_set",
    eligible_for_paper_simulation: true,
    settlement_equivalent: true,
  },
];

describe("Market Comparison operator decision", () => {
  it("uses Rejected as the primary decision when MB↔K is solver-evaluated and PM is leftover binary", () => {
    const decision = operatorDecision(matchResultPartial);
    expect(decision.label).toBe("Rejected");
    expect(decision.label).not.toBe("Matched equivalent");
    expect(decision.label).not.toBe("Partially comparable");
    expect(decision.tone).toBe("rejected");
    expect(decisionBadgeClass(decision.tone)).toContain("is-reject");
    expect(decisionBadgeClass(decision.tone)).not.toContain("is-hot");
    expect(decisionBadgeClass(decision.tone)).not.toContain("is-watch");
  });

  it("uses Paper eligible only when solver_is_arbitrage is true", () => {
    const decision = operatorDecision(qualified);
    expect(decision).toEqual({
      status: "paper_eligible",
      label: "Paper eligible",
      tone: "eligible",
    });
    expect(decisionBadgeClass(decision.tone)).toContain("is-hot");
  });

  it("does not treat radar-current rows as paper eligible", () => {
    const decision = operatorDecision({
      ...qualified,
      radar_freshness: "radar_current",
    });
    expect(decision.status).not.toBe("paper_eligible");
    expect(decision.label).toBe("Radar current");
    expect(decision.tone).toBe("caution");
  });

  it("labels a single venue as Venue only", () => {
    const decision = operatorDecision(
      row({
        comparison_status: "venue_only",
        matchbook: facts("matchbook", homeDrawAway),
      }),
    );
    expect(decision.label).toBe("Venue only");
    expect(decision.tone).toBe("neutral");
  });

  it("labels below-trigger evaluated economics as Near trigger", () => {
    const decision = operatorDecision(
      row({
        comparison_status: "matched_equivalent",
        entered_solver: true,
        solver_is_arbitrage: false,
        current_net_edge: 0.008,
        trigger_net_edge: 0.01,
        distance_to_trigger_pp: 0.2,
        matchbook: facts("matchbook", homeDrawAway),
        kalshi: facts("kalshi", homeDrawAway),
        pair_results: [
          {
            left_venue: "matchbook",
            right_venue: "kalshi",
            entered_solver: true,
            rejection_reasons: [],
            solver_is_arbitrage: false,
          },
        ],
      }),
    );
    expect(decision.label).toBe("Near trigger");
    expect(decision.tone).toBe("caution");
  });

  it("rejects negative-net fully comparable rows instead of treating mapping as the operator decision", () => {
    const decision = operatorDecision(
      row({
        comparison_status: "matched_equivalent",
        entered_solver: true,
        solver_is_arbitrage: false,
        current_net_edge: -0.0194,
        trigger_net_edge: 0.01,
        matchbook: facts("matchbook", homeDrawAway),
        kalshi: facts("kalshi", homeDrawAway),
        pair_results: [
          {
            left_venue: "matchbook",
            right_venue: "kalshi",
            entered_solver: true,
            rejection_reasons: [],
            solver_is_arbitrage: false,
          },
        ],
      }),
    );
    expect(decision.label).toBe("Rejected");
    expect(decision.tone).toBe("rejected");
  });

  it("keeps a positive-edge stale/fee gate as Rejected even when pair topology is only partial", () => {
    const view = inventoryCardViewModel(
      row({
        comparison_status: "matched_equivalent",
        entered_solver: true,
        solver_is_arbitrage: false,
        current_net_edge: 0.008,
        trigger_net_edge: 0.01,
        distance_to_trigger_pp: 0.2,
        reason: "stale_quote",
        rejection_reasons: ["stale_quote"],
        matchbook: facts("matchbook", homeDrawAway),
        polymarket: facts("polymarket", yesNo),
        kalshi: facts("kalshi", homeDrawAway),
        pair_results: [
          {
            left_venue: "matchbook",
            right_venue: "kalshi",
            entered_solver: true,
            rejection_reasons: [],
            solver_is_arbitrage: false,
          },
          {
            left_venue: "polymarket",
            right_venue: "kalshi",
            entered_solver: false,
            rejection_reasons: ["incomplete_outcome_set"],
            solver_is_arbitrage: false,
          },
        ],
      }),
    );
    expect(view.decision.label).toBe("Rejected");
    expect(view.decision.tone).toBe("rejected");
    expect(view.economics?.tone).not.toBe("eligible");
    expect(view.economics?.tone).not.toBe("caution");
    expect(view.comparableHeadline).toBe("Comparable: Matchbook ↔ Kalshi");
    expect(view.pairBadges.map((badge) => badge.text)).toEqual([
      "MB ↔ K · equivalent",
      "PM · incompatible outcome set",
    ]);
  });

  it("uses Partially comparable only when mapping is mixed and no solver decision exists", () => {
    const decision = operatorDecision(
      row({
        comparison_status: "matched_equivalent",
        entered_solver: false,
        solver_is_arbitrage: false,
        matchbook: facts("matchbook", homeDrawAway),
        polymarket: facts("polymarket", yesNo),
        kalshi: facts("kalshi", homeDrawAway),
        pair_results: [
          {
            left_venue: "matchbook",
            right_venue: "kalshi",
            entered_solver: false,
            rejection_reasons: [],
            solver_is_arbitrage: false,
          },
          {
            left_venue: "polymarket",
            right_venue: "kalshi",
            entered_solver: false,
            rejection_reasons: ["incomplete_outcome_set"],
            solver_is_arbitrage: false,
          },
        ],
      }),
    );
    expect(decision.label).toBe("Partially comparable");
    expect(decision.tone).toBe("caution");
  });
});

describe("pair truth and incompatible venues", () => {
  it("headlines the comparable pair and treats Polymarket binary as leftover", () => {
    const view = inventoryCardViewModel(matchResultPartial);
    expect(view.decision.label).toBe("Rejected");
    expect(view.title).toBe("Match Result · Full time");
    expect(view.comparableHeadline).toBe("Comparable: Matchbook ↔ Kalshi");
    expect(view.pairBadges.map((badge) => badge.text)).toEqual([
      "MB ↔ K · equivalent",
      "PM · incompatible outcome set",
    ]);
    expect(view.pairBadges.find((badge) => badge.comparable)?.tone).toBe("neutral");
    expect(view.discoveredNotes).toContain("Polymarket not comparable — binary market");
    const polymarket = view.venues.find((venue) => venue.venue === "polymarket");
    expect(polymarket?.kind).toBe("Binary");
    expect(polymarket?.incompatibility).toBe("Not comparable with 1X2");
    expect(polymarket?.quotes).toEqual(["YES 2.38 · $18,389", "NO 1.69 · $9,452"]);
  });
});

describe("compact economics and rejected-vs-qualified coloring", () => {
  it("shows compact net/trigger/status and does not paint rejected economics green", () => {
    const view = inventoryCardViewModel(matchResultPartial);
    expect(view.economics?.text).toBe("Net -1.94% | Trigger 1.00% | Rejected");
    expect(view.economics?.tone).toBe("rejected");
    expect(toneClass(view.economics!.tone)).toBe("is-rejected");
    expect(toneClass(view.economics!.tone)).not.toBe("is-eligible");
  });

  it("paints qualified economics eligible/green", () => {
    const view = inventoryCardViewModel(qualified, preparable);
    expect(view.economics?.text).toBe("Net 1.20% | Trigger 1.00% | Paper eligible");
    expect(view.economics?.tone).toBe("eligible");
    expect(toneClass(view.economics!.tone)).toBe("is-eligible");
  });

  it("keeps a positive net from looking actionable when the row is rejected", () => {
    const view = inventoryCardViewModel(
      row({
        comparison_status: "matched_equivalent",
        entered_solver: true,
        solver_is_arbitrage: false,
        current_net_edge: 0.0043,
        trigger_net_edge: 0.01,
        distance_to_trigger_pp: -1,
        reason: "no_arbitrage",
        matchbook: facts("matchbook", homeDrawAway),
        kalshi: facts("kalshi", homeDrawAway),
        pair_results: [
          {
            left_venue: "matchbook",
            right_venue: "kalshi",
            entered_solver: true,
            rejection_reasons: [],
            solver_is_arbitrage: false,
          },
        ],
      }),
    );
    expect(view.decision.label).toBe("Rejected");
    expect(view.economics?.text).toContain("Net 0.43%");
    expect(view.economics?.tone).not.toBe("eligible");
  });
});

describe("compact venue mini-cards", () => {
  it("formats Matchbook 1X2 quotes, fee and depth without technical health copy", () => {
    const matchbook = facts("matchbook", homeDrawAway, { usable_depth_at_touch: 450 });
    expect(compactQuoteLines(matchbook)).toEqual([
      "HOME 2.40 · £450",
      "DRAW 3.65 · £5,542",
      "AWAY 3.15 · £645",
    ]);
    expect(compactVenueMeta(matchbook)).toBe("Fee 2% · Depth £450");
    expect(failingVenueChecks(matchbook)).toEqual([]);
  });

  it("keeps missing FX prominent while healthy FX stays off the mini-card", () => {
    const missing = facts("polymarket", yesNo, { fx_status: "missing", settlement_complete: true });
    expect(compactVenueMeta(missing)).toContain("FX missing");
    expect(failingVenueChecks(missing)).toContain("FX missing");
    const healthy = facts("polymarket", yesNo, { fx_status: "known", settlement_complete: true });
    expect(compactVenueMeta(healthy)).not.toContain("FX");
    expect(failingVenueChecks(healthy).join(" ")).not.toContain("Settlement fingerprint complete");
  });

  it("names registered NFL settlement instead of an unknown fingerprint", () => {
    const paper = facts("kalshi", yesNo, {
      settlement_complete: false,
      settlement_status: "paper_assumed",
      settlement_provenance: "exceptional_settlement_mismatch_possible",
    });
    expect(settlementLabel(paper)).toContain("Registered equivalent");
    expect(settlementLabel(paper).toLowerCase()).not.toContain("never live-execution");
    expect(settlementLabel(paper)).not.toContain("incomplete/unknown");
    expect(failingVenueChecks(paper).join(" ")).not.toContain("incomplete/unknown");
    const unknown = facts("kalshi", yesNo, {
      settlement_complete: false,
      settlement_status: "incomplete",
    });
    expect(failingVenueChecks(unknown)).toContain("Settlement fingerprint incomplete/unknown");
  });
});

describe("paper action CTA visibility", () => {
  it("shows Review paper deployment for a qualifying preparable card", () => {
    const action = paperActionForRow(qualified, preparable);
    expect(action.eligible).toBe(true);
    expect(action.label).toBe("Review paper deployment →");
    expect(action.href).toBe("#paper-deployment");
    const view = inventoryCardViewModel(qualified, preparable);
    expect(view.paperAction).toEqual(action);
  });

  it("hides the positive CTA on rejected and non-preparable cards", () => {
    const rejected = paperActionForRow(matchResultPartial, preparable);
    expect(rejected.eligible).toBe(false);
    expect(rejected.label).toBe("Not eligible for deployment");
    expect(rejected.href).toBeNull();
    const noPlan = paperActionForRow(qualified, []);
    expect(noPlan.eligible).toBe(false);
    expect(noPlan.label).toBe("Not eligible for deployment");
  });

  it("hides the positive CTA when current allocator deployability is false", () => {
    const blocked = paperActionForRow(qualified, [
      { ...preparable[0], bet_actionable: false, bet_blocked_reason: "native available matchbook GBP" },
    ]);
    expect(blocked.eligible).toBe(false);
    expect(blocked.href).toBeNull();
  });
});

describe("sport-aware approved families", () => {
  it("keeps football rows primary and limits NFL and MLB to Stage-1 families", () => {
    expect(isPrimaryApprovedFamily("football", "match_result")).toBe(true);
    expect(isPrimaryApprovedFamily("american_football", "game_winner")).toBe(true);
    expect(isPrimaryApprovedFamily("american_football", "point_spread")).toBe(true);
    expect(isPrimaryApprovedFamily("american_football", "total_points")).toBe(true);
    expect(isPrimaryApprovedFamily("american_football", "match_result")).toBe(false);
    expect(isPrimaryApprovedFamily("baseball", "game_winner")).toBe(true);
    expect(isPrimaryApprovedFamily("baseball", "total_runs")).toBe(true);
    expect(isPrimaryApprovedFamily("mlb", "first_team_to_score")).toBe(false);
  });

  it("labels admitted football and NFL rows as registered equivalents", () => {
    const twoWay = [quote("home", 1.91, 200), quote("away", 2.05, 200)];
    const football = inventoryCardViewModel(
      row({
        comparison_status: "paper_assumed_equivalent",
        matchbook: facts("matchbook", homeDrawAway),
        kalshi: facts("kalshi", homeDrawAway),
      }),
    );
    expect(football.discoveredNotes.join(" ")).toContain("Registered equivalent");
    expect(football.discoveredNotes.join(" ").toLowerCase()).not.toContain("never live-execution");
    const nfl = inventoryCardViewModel(
      row({
        family: "game_winner",
        display_name: "Game winner",
        comparison_status: "paper_assumed_equivalent",
        matchbook: facts("matchbook", twoWay, { family: "game_winner" }),
        kalshi: facts("kalshi", twoWay, { family: "game_winner" }),
      }),
    );
    expect(nfl.discoveredNotes.join(" ")).toContain("Registered equivalent");
    expect(nfl.discoveredNotes.join(" ").toLowerCase()).not.toContain("never live-execution");
    expect(nfl.title.toLowerCase()).toContain("game winner");
  });
});
