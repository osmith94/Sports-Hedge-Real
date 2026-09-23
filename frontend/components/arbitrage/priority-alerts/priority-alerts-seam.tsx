import Link from "next/link";

export function PriorityAlertsSeam({
  liveAvailable = false,
  liveCount = 0,
}: {
  liveAvailable?: boolean;
  liveCount?: number;
}) {
  if (!liveAvailable || liveCount === 0) return null;

  return (
    <section className="pa-seam">
      <div className="pa-seam-copy">
        <div className="pa-seam-kicker">Priority Alerts · PAPER MODE</div>
        <div className="pa-seam-title">
          {`${liveCount} live paper alert${liveCount === 1 ? "" : "s"}`}
        </div>
        <p>Live paper alerts available.</p>
      </div>
      <div className="pa-seam-actions">
        <span className="status-badge">LIVE PAPER</span>
        <Link className="pa-button" href="/arbitrage/priority-alerts">
          All priority alerts
        </Link>
      </div>
    </section>
  );
}
