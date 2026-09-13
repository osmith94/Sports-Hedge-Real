"use client";

import { UnwindDecision } from "../lib/api";
import { money } from "../lib/format";

export function HoldVsUnwindCard({
  decision = null,
  emptyHint,
}: {
  decision?: UnwindDecision | null;
  emptyHint?: string;
}) {
  if (!decision) {
    return (
      <div className="empty-live">
        {emptyHint ??
          "Hold versus unwind is analytical only. Reverse-side quotes are required; a clock estimate never releases capital."}
      </div>
    );
  }

  const ttr = decision.estimated_time_to_release;
  return (
    <div className="hold-card">
      <div className="hold-top">
        <div>
          <div className="panel-title">Hold vs clean unwind</div>
          <div className="panel-meta">
            Modelled paper close plan. Conditionally releasable is not spendable.
          </div>
        </div>
        <span className="demo-chip">{decision.recommendation.replaceAll("_", " ")}</span>
      </div>
      <p className="section-copy">{decision.decision_reason.replaceAll("_", " ")}</p>
      <div className="hold-grid">
        <div>
          <div className="metric-label">Hold-to-settlement P&amp;L</div>
          <div className="metric-value" style={{ fontSize: 18 }}>{money(decision.hold_pnl_gbp)}</div>
        </div>
        <div>
          <div className="metric-label">Validated exit P&amp;L</div>
          <div className="metric-value" style={{ fontSize: 18 }}>{money(decision.validated_exit_pnl_gbp)}</div>
        </div>
        <div>
          <div className="metric-label">Unwind cost</div>
          <div className="metric-value" style={{ fontSize: 18 }}>{money(decision.unwind_cost_gbp)}</div>
        </div>
        <div>
          <div className="metric-label">Close executable</div>
          <div className="metric-value" style={{ fontSize: 18 }}>
            {decision.close_plan?.fully_executable ? "Yes" : "No"}
          </div>
        </div>
      </div>
      <div className="scan-note">
        Modelled time-to-release: {ttr.source_class}/{ttr.basis}
        {ttr.remaining_lock_minutes != null ? ` · ${ttr.remaining_lock_minutes} min` : " · unknown"}
        {ttr.confidence != null ? ` · confidence ${ttr.confidence}` : ""}. Advisory only; this
        estimate does not release capital. Spendable release requires{" "}
        {decision.spendable_release_requires.replaceAll("_", " ")}.
      </div>
      {decision.close_plan?.rejection_reasons?.length ? (
        <div className="scan-note">
          Close rejections: {decision.close_plan.rejection_reasons.join(", ")}
        </div>
      ) : null}
    </div>
  );
}
