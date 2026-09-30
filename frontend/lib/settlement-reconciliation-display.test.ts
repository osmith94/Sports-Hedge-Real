import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import type { PaperTrade } from "./api";
import { blockerLabel, settlementReconciliationLabel } from "./settlement-reconciliation-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function trade(overrides: Partial<PaperTrade> = {}): PaperTrade {
  return {
    trade_id: "ptrade-1",
    opportunity_id: "opp-1",
    market_family: "match_result",
    home_team: "Newcastle",
    away_team: "Arsenal",
    state: "OPEN",
    opened_at: "2026-09-20T17:00:00.000Z",
    last_updated_at: "2026-09-20T17:00:00.000Z",
    capital_locked_native: { GBP: "10" },
    provenance: "live_paper",
    paper_only: true,
    places_orders: false,
    legs: [],
    audit: [],
    ...overrides,
  };
}

describe("settlement reconciliation display", () => {
  it("labels blocked auto-settlement without implying a kickoff result", () => {
    const row = trade({
      settlement_reconciliation_status: "blocked",
      settlement_blocker: "incomplete_provider_result",
      last_settlement_check_at: "2026-09-21T07:00:00.000Z",
    });
    expect(settlementReconciliationLabel(row)).toBe(
      "Auto-settlement blocked · Incomplete provider result",
    );
    expect(blockerLabel("provider_status_postponed")).toBe("Provider status · postponed");
    expect(blockerLabel("provider_unavailable")).toBe("Provider unavailable");
    expect(blockerLabel("exceptional_settlement_mismatch_possible")).toBe(
      "Historical exceptional settlement caveat",
    );
    expect(blockerLabel("nfl_exceptional_tie_fail_closed")).toBe("NFL exceptional tie — fail closed");
    expect(blockerLabel("canonical_outcome_not_determined")).toBe("Canonical outcome not determined");
  });

  it("operator failsafe asks for a canonical result, not source/source id", () => {
    const book = readFileSync(join(frontendRoot, "components/paper-trade-book.tsx"), "utf8");
    expect(book).toMatch(/Settle completed trade/);
    expect(book).toMatch(/does not use current market quotes/);
    expect(book).not.toMatch(/Manual close \/ settle result/);
    expect(book).toMatch(/Confirm result & close trade/);
    expect(book).toMatch(/Actual canonical market result/);
    expect(book).not.toMatch(/name="source_id"/);
    expect(book).not.toMatch(/provenance: "fixture_demo"/);
    expect(book).toMatch(/manualSettlePaperTrade/);
  });
});
