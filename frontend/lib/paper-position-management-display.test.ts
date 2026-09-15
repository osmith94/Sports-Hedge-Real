import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { PositionManagementSnapshot } from "./api";
import {
  AUTHORITATIVE_RELEASE_CONTEXT,
  formatPositionManagementCell,
} from "./paper-position-management-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function snapshot(
  overrides: Partial<PositionManagementSnapshot> = {},
): PositionManagementSnapshot {
  return {
    trade_id: "ptrade-1",
    recommendation: "HOLD",
    decision_reason: "exit_inferior_to_hold_after_fees",
    evaluated_at: "2026-09-15T12:00:00.000Z",
    hold_pnl_gbp: "10",
    validated_exit_pnl_gbp: "9.44",
    unwind_cost_gbp: "0.56",
    remaining_lock_advisory: true,
    spendable: false,
    paper_only: true,
    places_orders: false,
    ...overrides,
  };
}

describe("active-trade position management copy", () => {
  it("shows HOLD economics and modelled ETA on the visible cell, not tooltip-only", () => {
    const cell = formatPositionManagementCell(
      snapshot({
        remaining_lock_minutes: "90",
        remaining_lock_basis: "modelled",
        remaining_lock_source_class: "modelled",
        remaining_lock_confidence: "0.40",
        remaining_lock_detail: "8C modelled estimate; advisory only; does not release capital",
      }),
    );
    assert.equal(cell.state, "HOLD");
    assert.match(cell.economics, /hold/);
    assert.match(cell.economics, /close-now/);
    assert.match(cell.economics, /give-up/);
    assert.match(cell.release, new RegExp(AUTHORITATIVE_RELEASE_CONTEXT));
    assert.match(cell.release, /modelled ETA 90m/);
    assert.match(cell.release, /basis modelled/);
    assert.match(cell.release, /confidence 0.40/);
    assert.match(cell.release, /8C modelled estimate/);
    assert.match(cell.release, /advisory, not spendable/);
    assert.doesNotMatch(cell.release, /spendable cash/i);

    const source = readFileSync(join(frontendRoot, "components/paper-trade-book.tsx"), "utf8");
    assert.match(source, /formatPositionManagementCell/);
    assert.match(source, /cell\.economics/);
    assert.match(source, /cell\.release/);
    assert.match(source, /function nativeLocked/);
    assert.match(source, /Active trades/);
    assert.match(source, /Open paper positions|Management/);
  });

  it("labels unknown remaining-lock ETA while still naming authoritative settlement", () => {
    const cell = formatPositionManagementCell(
      snapshot({
        validated_exit_pnl_gbp: null,
        unwind_cost_gbp: null,
        remaining_lock_basis: "unknown",
        remaining_lock_source_class: "unknown",
      }),
    );
    assert.equal(cell.state, "HOLD");
    assert.match(cell.economics, /close-now n\/a/);
    assert.match(cell.economics, /give-up n\/a/);
    assert.equal(
      cell.release,
      `${AUTHORITATIVE_RELEASE_CONTEXT} · ETA unknown · advisory, not spendable`,
    );
  });
});
