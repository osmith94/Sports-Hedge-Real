import Link from "next/link";

import { getOpenPriorityAlertCount, listPriorityAlerts } from "../../../lib/priority-alerts/provider";
import { money, percent } from "../../../lib/priority-alerts/format";

export function PriorityAlertsSeam() {
  const alerts = listPriorityAlerts();
  const critical = alerts.find((alert) => alert.severity === "CRITICAL") ?? alerts[0];
  const count = getOpenPriorityAlertCount();

  if (!critical) return null;

  return (
    <section className="pa-seam">
      <div className="pa-seam-copy">
        <div className="pa-seam-kicker">Priority Alerts · PAPER MODE</div>
        <div className="pa-seam-title">
          {count} exceptional {count === 1 ? "candidate" : "candidates"} above standing auto pools
        </div>
        <p>
          {critical.event.homeTeam} v {critical.event.awayTeam}: {percent(critical.netGuaranteedEdge)} net edge,{" "}
          {money(critical.recommendedSizeGbp, "GBP")} recommended, limiting depth {money(critical.rawLimitingDepthGbp, "GBP")}.
        </p>
      </div>
      <div className="pa-seam-actions">
        <span className="pa-chip pa-chip-demo">DEMO/FIXTURE DATA</span>
        <Link className="pa-button pa-button-primary" href={`/arbitrage/priority-alerts/${critical.alertId}`}>
          Open {critical.severity.replaceAll("_", " ")} alert
        </Link>
        <Link className="pa-button" href="/arbitrage/priority-alerts">
          All priority alerts
        </Link>
      </div>
    </section>
  );
}
