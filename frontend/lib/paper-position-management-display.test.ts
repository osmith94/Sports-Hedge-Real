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
    hold_pnl_gbp: "1.25",
    validated_exit_pnl_gbp: "0.84",
    unwind_cost_gbp: "0.41",
    exit_margin_gbp: "-0.21",
    exit_threshold_gbp: "0.20",
    exit_margin_basis: "abundant_capital_give_up",
    exit_margin_actionable: true,
    close_executable: true,
    remaining_lock_advisory: true,
    spendable: false,
    paper_only: true,
    places_orders: false,
    ...overrides,
  };
}

describe("active-trade position management copy", () => {
  it("shows HOLD economics, last check, and negative exit margin on the visible cell", () => {
    const cell = formatPositionManagementCell(
      snapshot({
        remaining_lock_minutes: "90",
        remaining_lock_basis: "modelled",
        remaining_lock_source_class: "modelled",
        remaining_lock_confidence: "0.40",
        remaining_lock_detail: "8C modelled estimate; advisory only; does not release capital",
      }),
    );
    assert.equal(cell.state, "CLOSURE: HOLD");
    assert.equal(cell.checkedIso, "2026-09-15T12:00:00.000Z");
    assert.match(cell.economics, /close-now/);
    assert.match(cell.economics, /hold/);
    assert.match(cell.threshold ?? "", /give-up/);
    assert.match(cell.threshold ?? "", /allowed/);
    assert.match(cell.margin, /EXIT MARGIN/);
    assert.match(cell.margin, /-/);
    assert.equal(cell.blocker, null);
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
    assert.match(source, /cell\.threshold/);
    assert.match(source, /cell\.margin/);
    assert.match(source, /cell\.blocker/);
    assert.match(source, /cell\.release/);
    assert.match(source, /HydratedRelativeTime/);
    assert.match(source, /prefix="checked"/);
    assert.match(source, /function nativeLocked/);
    assert.match(source, /Active trades/);
    assert.match(source, /Open paper positions|Management/);
    assert.match(source, /modelled economic distance to the unwind threshold/);
  });

  it("shows ELIGIBLE with non-negative exit margin and the allowed threshold", () => {
    const cell = formatPositionManagementCell(
      snapshot({
        recommendation: "UNWIND_ELIGIBLE",
        decision_reason: "give_up_within_abundant_threshold",
        hold_pnl_gbp: "1.25",
        validated_exit_pnl_gbp: "1.12",
        unwind_cost_gbp: "0.13",
        exit_margin_gbp: "0.07",
        exit_threshold_gbp: "0.20",
        exit_margin_actionable: true,
      }),
    );
    assert.equal(cell.state, "CLOSURE: ELIGIBLE");
    assert.equal(cell.checkedIso, "2026-09-15T12:00:00.000Z");
    assert.match(cell.economics, /close-now/);
    assert.match(cell.economics, /hold/);
    assert.match(cell.threshold ?? "", /give-up/);
    assert.match(cell.threshold ?? "", /allowed/);
    assert.match(cell.margin, /EXIT MARGIN/);
    assert.match(cell.margin, /\+/);
    assert.equal(cell.blocker, null);
  });

  it("uses opportunity-cost and scarce-capital copy from persisted basis, not a parallel rule", () => {
    const opportunity = formatPositionManagementCell(
      snapshot({
        recommendation: "UNWIND_ELIGIBLE",
        decision_reason: "unwind_cost_within_supplied_opportunity_cost",
        opportunity_cost_gbp: "0.50",
        unwind_cost_gbp: "0.10",
        exit_margin_gbp: "0.40",
        exit_threshold_gbp: "0.50",
        exit_margin_basis: "supplied_opportunity_cost",
        exit_margin_actionable: true,
      }),
    );
    assert.equal(opportunity.state, "CLOSURE: ELIGIBLE");
    assert.match(opportunity.margin, /EXIT MARGIN/);
    assert.match(opportunity.threshold ?? "", /allowed/);

    const scarce = formatPositionManagementCell(
      snapshot({
        capital_pressure: "scarce",
        recommendation: "HOLD",
        decision_reason: "give_up_exceeds_scarce_capital_threshold",
        exit_margin_gbp: "-0.35",
        exit_threshold_gbp: "0.15",
        exit_margin_basis: "scarce_capital_bounded_give_up",
        exit_margin_actionable: true,
      }),
    );
    assert.equal(scarce.state, "CLOSURE: HOLD");
    assert.match(scarce.margin, /EXIT MARGIN/);
    assert.match(scarce.margin, /-/);
  });

  it("labels unknown remaining-lock ETA while still naming authoritative settlement", () => {
    const cell = formatPositionManagementCell(
      snapshot({
        validated_exit_pnl_gbp: null,
        unwind_cost_gbp: null,
        exit_margin_gbp: null,
        exit_threshold_gbp: null,
        exit_margin_actionable: false,
        close_executable: false,
        remaining_lock_basis: "unknown",
        remaining_lock_source_class: "unknown",
      }),
    );
    assert.equal(cell.state, "CLOSURE: HOLD");
    assert.equal(cell.economics, "close-now unavailable");
    assert.equal(cell.margin, "EXIT MARGIN n/a");
    assert.equal(
      cell.release,
      `${AUTHORITATIVE_RELEASE_CONTEXT} · ETA unknown · advisory, not spendable`,
    );
  });

  it("names two-scan pending confirmation on the visible cell", () => {
    const cell = formatPositionManagementCell(
      snapshot({
        recommendation: "UNWIND_ELIGIBLE",
        auto_action: "unwind_pending_confirmation",
        exit_margin_gbp: "0.07",
        exit_threshold_gbp: "0.20",
        exit_margin_actionable: true,
      }),
    );
    assert.equal(cell.state, "CLOSURE: ELIGIBLE · PENDING CONFIRMATION");
    assert.match(cell.economics, /hold/);
    assert.match(cell.economics, /close-now/);
    assert.match(cell.release, new RegExp(AUTHORITATIVE_RELEASE_CONTEXT));
  });

  it("shows NOT SAFE with n/a margin and reverse-book blocker when no valid close plan exists", () => {
    const cell = formatPositionManagementCell(
      snapshot({
        recommendation: "UNWIND_NOT_SAFE",
        decision_reason: "missing_reverse_quote",
        close_blocker: "missing_reverse_quote",
        validated_exit_pnl_gbp: null,
        unwind_cost_gbp: null,
        exit_margin_gbp: null,
        exit_threshold_gbp: null,
        exit_margin_basis: "unavailable",
        exit_margin_actionable: false,
        close_executable: false,
      }),
    );
    assert.equal(cell.state, "CLOSURE: NOT SAFE");
    assert.equal(cell.checkedIso, "2026-09-15T12:00:00.000Z");
    assert.equal(cell.economics, "close-now unavailable");
    assert.equal(cell.threshold, null);
    assert.equal(cell.margin, "EXIT MARGIN n/a");
    assert.equal(cell.blocker, "blocked: stale/missing reverse-book evidence");
  });

  it("does not present a positive economic margin as permission when execution risk blocks the close", () => {
    const cell = formatPositionManagementCell(
      snapshot({
        recommendation: "UNWIND_NOT_SAFE",
        decision_reason: "close_execution_risk_exceeded",
        close_blocker: "close_execution_risk_exceeded",
        hold_pnl_gbp: "1.25",
        validated_exit_pnl_gbp: "1.12",
        unwind_cost_gbp: "0.13",
        exit_margin_gbp: "0.07",
        exit_threshold_gbp: "0.20",
        exit_margin_basis: "abundant_capital_give_up",
        exit_margin_actionable: false,
        close_executable: true,
      }),
    );
    assert.equal(cell.state, "CLOSURE: NOT SAFE");
    assert.match(cell.economics, /close-now/);
    assert.match(cell.threshold ?? "", /give-up/);
    assert.equal(cell.margin, "EXIT MARGIN n/a");
    assert.match(cell.blocker ?? "", /close execution risk exceeded/);
    assert.doesNotMatch(cell.margin, /\+/);
    assert.doesNotMatch(cell.state, /ELIGIBLE/);
  });
});
