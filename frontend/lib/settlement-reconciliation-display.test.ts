import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { PaperTrade } from "./api";
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
    assert.equal(
      settlementReconciliationLabel(row),
      "Auto-settlement blocked · Incomplete provider result",
    );
    assert.equal(blockerLabel("provider_status_postponed"), "Exceptional lifecycle · postponed");
    assert.equal(blockerLabel("provider_unavailable"), "Provider unavailable");
  });

  it("operator failsafe asks for a canonical result, not source/source id", () => {
    const book = readFileSync(join(frontendRoot, "components/paper-trade-book.tsx"), "utf8");
    assert.match(book, /Manual close \/ settle result/);
    assert.match(book, /Confirm result & close trade/);
    assert.match(book, /Actual canonical market result/);
    assert.doesNotMatch(book, /name="source_id"/);
    assert.doesNotMatch(book, /provenance: "fixture_demo"/);
    assert.match(book, /manualSettlePaperTrade/);
  });
});
