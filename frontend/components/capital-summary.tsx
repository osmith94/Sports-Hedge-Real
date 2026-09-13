import { PaperTradeBookSummary } from "../lib/api";
import { CapitalSnapshot } from "../lib/arbitrage-ops";
import { money } from "../lib/format";

export function CapitalSummary({
  live,
  tradeSummary = null,
}: {
  live: CapitalSnapshot;
  tradeSummary?: PaperTradeBookSummary | null;
}) {
  const realised = tradeSummary?.realised_pnl_gbp ?? null;
  const locked = tradeSummary?.capital_locked_gbp ?? null;
  const cards = [
    { label: "Realised P&L", value: realised, foot: "Closed paper trades" },
    { label: "Locked paper stake", value: locked, foot: "Open trades · GBP carrying" },
    { label: "Today P&L", value: live.todayPnlGbp, foot: "Unset until closed paper trades exist" },
  ];

  return (
    <section>
      <div className="section-label">
        <span>Activity P&amp;L</span>
        <span className="status-badge">PAPER ONLY</span>
      </div>
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
