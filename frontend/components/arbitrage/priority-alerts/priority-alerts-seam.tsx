import Link from "next/link";

import { listPriorityAlerts } from "../../../lib/priority-alerts/provider";

export function PriorityAlertsSeam({
  liveAvailable = false,
  liveCount = 0,
}: {
  liveAvailable?: boolean;
  liveCount?: number;
}) {
  const alerts = listPriorityAlerts();
  const critical = alerts.find((alert) => alert.severity === "CRITICAL") ?? alerts[0];

  if (!critical) return null;

  return (
    <section className="pa-seam">
      <div className="pa-seam-copy">
        <div className="pa-seam-kicker">Priority Alerts · PAPER MODE</div>
        <div className="pa-seam-title">
          {liveAvailable
            ? `${liveCount} live paper alert${liveCount === 1 ? "" : "s"}`
            : "Live priority-alert API unavailable"}
        </div>
        <p>
          Live backend `/priority-alerts` {liveAvailable ? (liveCount ? "has paper alerts." : "is empty — no demo substitution.") : "could not be reached."}
          {" "}A labelled DEMO walkthrough ticket remains for MANUAL_EXTERNAL: {critical.event.homeTeam} v {critical.event.awayTeam}.
        </p>
      </div>
      <div className="pa-seam-actions">
        <span className="pa-chip pa-chip-demo">DEMO WALKTHROUGH · NOT LIVE</span>
        <Link className="pa-button pa-button-primary" href={`/arbitrage/priority-alerts/${critical.alertId}`}>
          Open demo {critical.severity.replaceAll("_", " ")} alert
        </Link>
        <Link className="pa-button" href="/arbitrage/priority-alerts">
          All priority alerts
        </Link>
      </div>
    </section>
  );
}
