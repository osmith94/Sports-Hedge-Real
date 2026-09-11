import { trendCards } from "../../lib/mock-data";

const filters = ["Football", "Premier League", "Newcastle United", "All markets", "All venues", "Team-sheet events", "90m pre-kickoff"];

export default function TrendsPage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Trend Explorer</div>
          <h1>Historical betting-market cohorts</h1>
          <p className="page-subtitle">
            Compare price behaviour, event reactions, liquidity and reversion across teams, competitions, market families and start-price buckets.
          </p>
        </div>
        <div className="demo-label">ILLUSTRATIVE RESEARCH RESULTS</div>
      </div>

      <div className="filter-bar">
        {filters.map((filter, index) => (
          <span className={`filter-chip ${index < 3 ? "active" : ""}`} key={filter}>{filter}</span>
        ))}
      </div>

      <section className="metric-grid">
        <div className="metric-card"><div className="metric-label">Series analysed</div><div className="metric-value">1,284</div><div className="metric-foot">Current filter universe</div></div>
        <div className="metric-card"><div className="metric-label">Median open → close</div><div className="metric-value metric-positive">+1.8pp</div><div className="metric-foot">Selected market outcome</div></div>
        <div className="metric-card"><div className="metric-label">Median liquidity growth</div><div className="metric-value">+71%</div><div className="metric-foot">Opening snapshot to close</div></div>
        <div className="metric-card"><div className="metric-label">Stable trends</div><div className="metric-value">12</div><div className="metric-foot">Passed sample thresholds</div></div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Ranked research patterns</div>
            <div className="panel-meta">Effect size is shown with cohort size and stability—not as a prediction claim</div>
          </div>
        </div>
        <div className="panel-body trend-list">
          {trendCards.map((trend) => (
            <div className="trend-card" key={trend.title}>
              <div>
                <div className="trend-title">{trend.title}</div>
                <div className="trend-detail">{trend.detail}</div>
              </div>
              <div className="trend-stat"><div className="trend-stat-label">Effect</div><div className="trend-stat-value metric-positive">{trend.effect}</div></div>
              <div className="trend-stat"><div className="trend-stat-label">Sample</div><div className="trend-stat-value">{trend.sample}</div></div>
              <div className="trend-stat"><div className="trend-stat-label">Stability</div><div className="trend-stat-value">{trend.stability}</div></div>
            </div>
          ))}
        </div>
      </section>

      <div style={{ height: 14 }} />
      <section className="grid-equal">
        <div className="panel">
          <div className="panel-header"><div className="panel-title">Available metric families</div><div className="panel-meta">Backend contract</div></div>
          <div className="panel-body reaction-list">
            {[
              "Opening → closing probability movement",
              "Realized logit volatility",
              "Spread compression / expansion",
              "Liquidity growth",
              "Peak move retracement",
            ].map((metric) => <div className="reaction-row" key={metric}><div className="reaction-name">{metric}</div></div>)}
          </div>
        </div>
        <div className="panel">
          <div className="panel-header"><div className="panel-title">Statistical guardrails</div><div className="panel-meta">Always visible</div></div>
          <div className="panel-body">
            <div className="empty-live">
              Every trend must expose sample size, cohort definition and stability. Thin samples are flagged. Event timing is association rather than proof of causation, and patterns explored through many filters should later carry a data-mining / multiple-comparison warning.
            </div>
          </div>
        </div>
      </section>
    </>
  );
}
