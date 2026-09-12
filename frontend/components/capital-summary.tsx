import { CapitalSnapshot } from "../lib/arbitrage-ops";
import { money } from "../lib/format";

export function CapitalSummary({
  live,
  fixture,
}: {
  live: CapitalSnapshot;
  fixture: CapitalSnapshot;
}) {
  const cards = [
    { label: "Realised P&L", value: fixture.realisedPnlGbp, live: live.realisedPnlGbp, foot: "Arbitrage strategy · paper", demo: live.realisedPnlGbp === null },
    { label: "Locked capital", value: live.lockedCapitalGbp ?? fixture.lockedCapitalGbp, live: live.lockedCapitalGbp, foot: "Open paper exposure", demo: live.lockedCapitalGbp === null },
    { label: "Available capital", value: live.availableCapitalGbp ?? fixture.availableCapitalGbp, live: live.availableCapitalGbp, foot: "Residual vs capital limit", demo: live.availableCapitalGbp === null },
    { label: "Today / MTD / all-time", value: fixture.todayPnlGbp, live: live.todayPnlGbp, foot: `${money(fixture.todayPnlGbp)} · ${money(fixture.mtdPnlGbp)} · ${money(fixture.allTimePnlGbp)}`, demo: true },
  ];

  return (
    <section>
      <div className="section-label">
        <span>Running P&amp;L / capital</span>
        <span className="demo-chip">PAPER ONLY · MIXED LIVE / DEMO</span>
      </div>
      <div className="metric-grid metric-grid-compact">
        {cards.map((card) => (
          <div className="metric-card" key={card.label}>
            <div className="metric-label">
              {card.label}
              {card.demo ? <span className="demo-inline">DEMO</span> : <span className="live-inline">LIVE</span>}
            </div>
            <div className={`metric-value ${card.label.includes("P&L") ? "metric-positive" : ""}`}>
              {money(card.demo ? card.value : card.live ?? card.value)}
            </div>
            <div className="metric-foot">{card.foot}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
