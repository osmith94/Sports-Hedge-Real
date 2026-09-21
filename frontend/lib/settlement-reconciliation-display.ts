import { PaperTrade } from "./api";

const BLOCKER_LABELS: Record<string, string> = {
  missing_durable_provider_identity: "Missing durable provider identity",
  incomplete_provider_result: "Incomplete provider result",
  provider_unavailable: "Provider unavailable",
  conflicting_provider_results: "Conflicting provider results",
  settlement_cycle_error: "Settlement cycle error",
  unsupported_market_family: "Unsupported market family",
  unsupported_manual_result_family: "Unsupported manual-result family",
  nfl_exceptional_tie_fail_closed: "NFL exceptional tie — fail closed",
  void_matchbook_runner: "Voided Matchbook runner",
};

export function settlementReconciliationLabel(trade: PaperTrade): string | null {
  if (trade.state === "CLOSED") {
    return trade.settlement_outcome
      ? `Settled ${trade.settlement_outcome}`
      : "Settled";
  }
  const status = trade.settlement_reconciliation_status;
  if (status === "blocked") {
    const blocker = trade.settlement_blocker
      ? blockerLabel(trade.settlement_blocker)
      : "blocked";
    return `Auto-settlement blocked · ${blocker}`;
  }
  if (status === "ready") return "Auto-settlement ready";
  if (status === "settled") return "Settled";
  return null;
}

export function blockerLabel(reason: string): string {
  if (BLOCKER_LABELS[reason]) return BLOCKER_LABELS[reason];
  if (reason.startsWith("provider_status_")) {
    return `Exceptional lifecycle · ${reason.slice("provider_status_".length).replaceAll("_", " ")}`;
  }
  return reason.replaceAll("_", " ");
}
