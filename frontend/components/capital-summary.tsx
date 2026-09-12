import { CapitalSnapshot } from "../lib/arbitrage-ops";
import { money } from "../lib/format";

export function CapitalSummary({ live }: { live: CapitalSnapshot }) {
  const cards = [
    { label: "Realised P&L", value: live.realisedPnlGbp, foot: "Arbitrage strategy · not persisted yet" },
    { label: "Locked paper stake", value: live.lockedCapitalGbp, foot: "Eligible scan exposure · GBP" },
    { label: "Today P&L", value: live.todayPnlGbp, foot: "No combined USD+GBP cash figure" },
  ];

  return (
    <section>
      <div className="section-label">
        <span>Activity P&amp;L</span>
        <span className="status-badge">PAPER ONLY · NO FAKE TOTALS</span>
      </div>
      <p className="section-copy">Paper P&amp;L is shown only when the scan read model has it; standing capital lives in native pools.</p>
      <div className="metric-grid metric-grid-compact">
        {cards.map((card) => (
          <div className="metric-card" key={card.label}>
            <div className="metric-label">{card.label}</div>
            <div className={`metric-value ${card.label.includes("P&L") ? "metric-positive" : ""}`}>
              {money(card.value)}
            </div>
            <div className="metric-foot">{card.foot}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
