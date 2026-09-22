import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { PaperTrade } from "./api";
import {
  formatCurrentExit,
  formatEntryArb,
  formatExitDelta,
  exitBlockReasonText,
} from "./active-trade-exit-display";

const here = dirname(fileURLToPath(import.meta.url));

function trade(overrides: Partial<PaperTrade> = {}): PaperTrade {
  return {
    trade_id: "ptrade-512",
    opportunity_id: "opp-512",
    state: "OPEN",
    opened_at: "2026-09-15T12:00:00.000Z",
    last_updated_at: "2026-09-15T12:00:00.000Z",
    capital_locked_native: { GBP: "100" },
    capital_locked_gbp: "100",
    provenance: "live_paper",
    paper_only: true,
    places_orders: false,
    legs: [],
    audit: [],
    entry_net_edge: "0.0182",
    current_exit_pct: "0.0064",
    current_exit_delta_pp: "-1.18",
    current_exit_checked_at: "2026-09-15T12:05:00.000Z",
    current_exit_block_reason: null,
    ...overrides,
  };
}

describe("active trade exit read model display", () => {
  it("renders entry arb, current exit and delta from backend fields", () => {
    const row = trade();
    assert.equal(formatEntryArb(row), "+1.82%");
    assert.equal(formatCurrentExit(row), "+0.64%");
    assert.equal(formatExitDelta(row), "-1.18pp");
  });

  it("shows an em dash and the exact block reason when exit economics are absent", () => {
    const row = trade({
      current_exit_pct: null,
      current_exit_delta_pp: null,
      current_exit_block_reason: "missing_reverse_quote",
    });
    assert.equal(formatCurrentExit(row), "—");
    assert.equal(formatExitDelta(row), "—");
    assert.equal(exitBlockReasonText(row), "missing reverse quote");
    assert.equal(formatEntryArb(row), "+1.82%");
  });

  it("keeps opening arb fixed when current exit economics change", () => {
    const opened = trade();
    const later = trade({
      current_exit_pct: "0.011",
      current_exit_delta_pp: "-0.72",
    });
    assert.equal(formatEntryArb(opened), formatEntryArb(later));
    assert.notEqual(formatCurrentExit(opened), formatCurrentExit(later));
  });

  it("wires compact active-trade columns without reconstructing exit economics", () => {
    const book = readFileSync(join(here, "..", "components/paper-trade-book.tsx"), "utf8");
    const display = readFileSync(join(here, "active-trade-exit-display.ts"), "utf8");
    assert.match(book, /Entry Arb %/);
    assert.match(book, /Current Exit %/);
    assert.match(book, /formatEntryArb/);
    assert.match(book, /formatCurrentExit/);
    assert.match(book, /formatExitDelta/);
    assert.match(book, /managementBadgeClass/);
    assert.match(book, /CLOSE AVAILABLE/);
    assert.match(book, /CLOSE BLOCKED/);
    assert.match(book, /ExitEvidence/);
    assert.doesNotMatch(display, /validated_exit_pnl/);
    assert.doesNotMatch(display, /capital_locked_gbp/);
    assert.doesNotMatch(book, /className="status-badge">\{cell\.state\}/);
  });
});
