import { getPriorityAlertProviderMeta } from "../../../lib/priority-alerts/provider";
import { PriorityAlertList } from "../../../components/arbitrage/priority-alerts/priority-alert-list";

export default function PriorityAlertsPage() {
  const meta = getPriorityAlertProviderMeta();

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Priority Arb Alerts</div>
          <h1>Exceptional paper opportunities</h1>
          <p className="page-subtitle">
            Escalation layer above standing automated liquidity pools. Deep-linkable for later email/push handoff.
            Phase 1 is paper-only: prepare a manual override ticket, never place a bet.
          </p>
        </div>
        <div className="pa-heading-flags">
          <div className="demo-label">PAPER MODE</div>
          <div className="demo-label">{meta.label}</div>
        </div>
      </div>
      <p className="pa-provider-note">{meta.note}</p>
      <PriorityAlertList />
    </>
  );
}
