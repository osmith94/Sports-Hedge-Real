import { PositionManagementSnapshot } from "./api";
import { money } from "./format";

export const AUTHORITATIVE_RELEASE_CONTEXT = "after authoritative settlement";

export type PositionManagementCellCopy = {
  state: string;
  economics: string;
  release: string;
};

export function formatPositionManagementCell(
  snapshot: PositionManagementSnapshot | null | undefined,
): PositionManagementCellCopy {
  if (!snapshot) {
    return {
      state: "—",
      economics: "No position-management evaluation yet.",
      release: `${AUTHORITATIVE_RELEASE_CONTEXT} · ETA unknown · advisory, not spendable`,
    };
  }
  const state =
    snapshot.recommendation === "UNWIND_ELIGIBLE"
      ? "UNWIND ELIGIBLE"
      : snapshot.recommendation === "UNWIND_NOT_SAFE"
        ? "NOT SAFE"
        : "HOLD";
  const closeNow =
    snapshot.validated_exit_pnl_gbp == null ? "n/a" : money(snapshot.validated_exit_pnl_gbp);
  const giveUp = snapshot.unwind_cost_gbp == null ? "n/a" : money(snapshot.unwind_cost_gbp);
  const economics = `hold ${money(snapshot.hold_pnl_gbp)} · close-now ${closeNow} · give-up ${giveUp}`;
  return {
    state,
    economics,
    release: formatReleaseContext(snapshot),
  };
}

export function formatReleaseContext(snapshot: PositionManagementSnapshot): string {
  const bits = [AUTHORITATIVE_RELEASE_CONTEXT];
  const minutes = snapshot.remaining_lock_minutes;
  const basis = snapshot.remaining_lock_basis;
  const knownEta =
    minutes != null &&
    basis != null &&
    basis !== "unknown" &&
    Number(minutes) >= 0;
  if (knownEta) {
    const classLabel = snapshot.remaining_lock_source_class || basis;
    bits.push(`${classLabel} ETA ${Number(minutes)}m`);
    bits.push(`basis ${String(basis).replaceAll("_", " ")}`);
    bits.push(
      snapshot.remaining_lock_confidence == null
        ? "confidence unknown"
        : `confidence ${snapshot.remaining_lock_confidence}`,
    );
  } else {
    bits.push("ETA unknown");
  }
  bits.push("advisory, not spendable");
  return bits.join(" · ");
}
