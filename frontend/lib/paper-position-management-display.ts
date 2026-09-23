import { PositionManagementSnapshot } from "./api";
import { money, number } from "./format";

export const AUTHORITATIVE_RELEASE_CONTEXT = "after authoritative settlement";

const REVERSE_BOOK_BLOCKERS = [
  "missing_reverse_quote",
  "stale_quote",
  "unknown_quote_age",
  "close_not_fully_executable",
  "insufficient_reverse_depth",
  "second_revalidation_missing_reverse_quote",
  "refresh_revalidation_needed",
  "revalidation_needed",
];

export type ManagementTone = "ready" | "waiting" | "unsafe" | "neutral";

export type PositionManagementCellCopy = {
  state: string;
  tone: ManagementTone;
  checkedIso: string | null;
  economics: string;
  threshold: string | null;
  margin: string;
  blocker: string | null;
  release: string;
};

const UNSAFE_TOKENS = ["execution_risk", "unsafe", "unhedged", "error", "failed"];

export function managementTone(
  snapshot: PositionManagementSnapshot | null | undefined,
): ManagementTone {
  if (!snapshot) return "neutral";
  const reason = `${snapshot.close_blocker ?? ""} ${snapshot.decision_reason ?? ""}`.toLowerCase();
  if (snapshot.recommendation === "UNWIND_NOT_SAFE") {
    return UNSAFE_TOKENS.some((token) => reason.includes(token)) ? "unsafe" : "waiting";
  }
  if (snapshot.recommendation === "UNWIND_ELIGIBLE" && snapshot.close_executable) return "ready";
  if (snapshot.recommendation === "HOLD") return "waiting";
  return "neutral";
}

export function managementBadgeClass(tone: ManagementTone): string {
  if (tone === "ready") return "status-badge";
  if (tone === "unsafe") return "status-badge status-badge-error";
  if (tone === "waiting") return "status-badge status-badge-warn";
  return "status-badge status-badge-stopped";
}

export function signedMoney(value: string | number | null | undefined): string {
  const parsed = number(value);
  if (parsed === null) return "n/a";
  const formatted = money(Math.abs(parsed));
  if (parsed > 0) return `+${formatted}`;
  if (parsed < 0) return `-${formatted}`;
  return formatted;
}

export function formatCloseBlocker(reason: string | null | undefined): string | null {
  if (!reason) return null;
  const normalised = reason.toLowerCase();
  if (
    REVERSE_BOOK_BLOCKERS.some((token) => normalised.includes(token)) ||
    normalised.includes("revalidation") ||
    normalised.includes("missing_reverse") ||
    normalised.includes("stale")
  ) {
    return "stale/missing reverse-book evidence";
  }
  return reason.replaceAll("_", " ");
}

function closureState(snapshot: PositionManagementSnapshot): string {
  const baseState =
    snapshot.recommendation === "UNWIND_ELIGIBLE"
      ? "ELIGIBLE"
      : snapshot.recommendation === "UNWIND_NOT_SAFE"
        ? "NOT SAFE"
        : "HOLD";
  const labelled = `CLOSURE: ${baseState}`;
  return snapshot.auto_action === "unwind_pending_confirmation"
    ? `${labelled} · PENDING CONFIRMATION`
    : labelled;
}

function closePlanAvailable(snapshot: PositionManagementSnapshot): boolean {
  return snapshot.validated_exit_pnl_gbp != null;
}

export function formatPositionManagementCell(
  snapshot: PositionManagementSnapshot | null | undefined,
): PositionManagementCellCopy {
  if (!snapshot) {
    return {
      state: "—",
      tone: "neutral",
      checkedIso: null,
      economics: "No position-management evaluation yet.",
      threshold: null,
      margin: "EXIT MARGIN n/a",
      blocker: null,
      release: `${AUTHORITATIVE_RELEASE_CONTEXT} · ETA unknown · advisory, not spendable`,
    };
  }

  const closeNowKnown = closePlanAvailable(snapshot);
  const holdKnown = snapshot.hold_pnl_gbp != null;
  const giveUpKnown = snapshot.unwind_cost_gbp != null;
  const allowedKnown = snapshot.exit_threshold_gbp != null;
  const marginKnown = snapshot.exit_margin_gbp != null;
  const actionable = snapshot.exit_margin_actionable === true;
  const blockerReason =
    snapshot.close_blocker ||
    (snapshot.recommendation === "UNWIND_NOT_SAFE" ? snapshot.decision_reason : null);

  let economics: string;
  let threshold: string | null = null;
  if (!closeNowKnown) {
    economics = "close-now unavailable";
  } else {
    const closeNow = money(snapshot.validated_exit_pnl_gbp);
    const hold = holdKnown ? money(snapshot.hold_pnl_gbp) : "n/a";
    economics = `close-now ${closeNow} · hold ${hold}`;
    const giveUp = giveUpKnown ? money(snapshot.unwind_cost_gbp) : "n/a";
    const allowed = allowedKnown ? money(snapshot.exit_threshold_gbp) : "n/a";
    threshold = `give-up ${giveUp} · allowed ${allowed}`;
  }

  const margin =
    marginKnown && actionable
      ? `EXIT MARGIN ${signedMoney(snapshot.exit_margin_gbp)}`
      : "EXIT MARGIN n/a";

  const blocker = formatCloseBlocker(blockerReason);

  return {
    state: closureState(snapshot),
    tone: managementTone(snapshot),
    checkedIso: snapshot.evaluated_at,
    economics,
    threshold,
    margin,
    blocker: blocker ? `blocked: ${blocker}` : null,
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
  if (snapshot.remaining_lock_detail) {
    bits.push(snapshot.remaining_lock_detail);
  }
  bits.push("advisory, not spendable");
  return bits.join(" · ");
}
