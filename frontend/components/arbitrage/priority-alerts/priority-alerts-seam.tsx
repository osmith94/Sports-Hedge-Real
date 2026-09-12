import Link from "next/link";

export function PriorityAlertsSeam({
  liveAvailable = false,
  liveCount = 0,
}: {
  liveAvailable?: boolean;
  liveCount?: number;
}) {
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
          {liveAvailable
            ? liveCount
              ? "Live paper alerts available."
              : "No live alerts."
            : "Priority-alert API unavailable."}
        </p>
      </div>
      <div className="pa-seam-actions">
        <span className={liveAvailable ? "status-badge" : "demo-chip"}>
          {liveAvailable ? "LIVE PAPER · NO DEMO MIX-IN" : "API UNAVAILABLE"}
        </span>
        <Link className="pa-button" href="/arbitrage/priority-alerts">
          All priority alerts
        </Link>
      </div>
    </section>
  );
}
