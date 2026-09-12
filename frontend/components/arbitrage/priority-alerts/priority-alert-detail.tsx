import { money, percent, quoteAge, venueLabel } from "../../../lib/priority-alerts/format";
import type { PriorityAlert } from "../../../lib/priority-alerts/types";
import { ManualOverrideTicket } from "./manual-override-ticket";
import { ExternalLegWorkflow } from "./external-leg-workflow";

export function PriorityAlertDetail({ alert }: { alert: PriorityAlert }) {
  const limiting = alert.legs.find((leg) => leg.isLimiting);

  return (
    <div className="pa-detail">
      <section className="pa-hero pa-hero-critical">
        <div className="pa-hero-flags">
          <span className={`pa-severity pa-severity-${alert.severity === "CRITICAL" ? "critical" : alert.severity === "HIGH_PRIORITY" ? "high" : "priority"}`}>
            {alert.severity.replaceAll("_", " ")}
          </span>
          <span className="pa-chip pa-chip-paper">PAPER MODE</span>
          <span className="pa-chip pa-chip-demo">DEMO/FIXTURE DATA</span>
        </div>
        <h1>
          {alert.event.homeTeam} v {alert.event.awayTeam}
        </h1>
        <p className="page-subtitle">
          {alert.event.competition} · {alert.market.label} · {alert.market.settlement} · kickoff{" "}
          {new Date(alert.event.kickoffUtc).toLocaleString("en-GB", { timeZone: "UTC" })} UTC
        </p>
        <p className="pa-hero-why">{alert.whyExceptional}</p>
        <dl className="pa-hero-metrics">
          <div>
            <dt>Net guaranteed edge</dt>
            <dd className="pa-accent">{percent(alert.netGuaranteedEdge)}</dd>
          </div>
          <div>
            <dt>Expected guaranteed profit</dt>
            <dd className="pa-accent">{money(alert.expectedProfitGbpAtRecommended, "GBP")}</dd>
          </div>
          <div>
            <dt>Recommended size</dt>
            <dd>{money(alert.recommendedSizeGbp, "GBP")}</dd>
          </div>
          <div>
            <dt>Validated maximum</dt>
            <dd>{money(alert.maxValidatedSizeGbp, "GBP")}</dd>
          </div>
        </dl>
      </section>

      <section className="pa-facts">
        <article>
          <h2>Event / market / settlement</h2>
          <ul>
            <li>Event {alert.event.canonicalEventId}</li>
            <li>Market {alert.market.canonicalMarketId}</li>
            <li>Period {alert.market.period.replaceAll("_", " ")}</li>
            <li>Settlement key {alert.market.settlementKey}</li>
            <li>Opportunity {alert.opportunityId}</li>
            <li>Deep link /arbitrage/priority-alerts/{alert.alertId}</li>
          </ul>
        </article>
        <article>
          <h2>Sizing honesty</h2>
          <ul>
            <li>Raw visible limiting depth {money(alert.rawLimitingDepthGbp, "GBP")}</li>
            <li>Safety haircut {percent(alert.safetyHaircut, 0)}</li>
            <li>Limiting leg {limiting ? `${venueLabel(limiting.venue)} ${limiting.selectionLabel}` : "—"}</li>
            <li>Max theoretical size {money(alert.maxTheoreticalSizeGbp, "GBP")}</li>
            <li>Quote age {quoteAge(alert.quoteAgeMs)}</li>
            <li>Execution risk {alert.executionRiskScore} · {alert.executionRiskBand}</li>
          </ul>
        </article>
        <article>
          <h2>Fill confidence · {alert.fillConfidence}</h2>
          <p className="pa-score">Score {(alert.fillConfidenceScore * 100).toFixed(0)} / 100 — operational estimate, not a fill guarantee.</p>
          <ul>
            {alert.fillConfidenceFactors.map((factor) => (
              <li key={factor.id}>
                {factor.label}: {factor.value} — {factor.note}
              </li>
            ))}
          </ul>
        </article>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Venues and legs</div>
            <div className="panel-meta">Visible depth by leg · recommended stakes at haircuted size</div>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Outcome</th>
                <th>Venue</th>
                <th>Odds</th>
                <th>Visible depth</th>
                <th>Depth GBP</th>
                <th>Recommended stake</th>
                <th>Quote age</th>
                <th>Levels</th>
              </tr>
            </thead>
            <tbody>
              {alert.legs.map((leg) => (
                <tr key={leg.legId} className={leg.isLimiting ? "pa-row-limit" : undefined}>
                  <td className="row-title">
                    {leg.selectionLabel}
                    {leg.isLimiting ? " · limiting" : ""}
                  </td>
                  <td>{venueLabel(leg.venue)} · {leg.currency}</td>
                  <td>{leg.decimalOdds.toFixed(3)}</td>
                  <td>{money(leg.visibleDepthNative, leg.currency)}</td>
                  <td>{money(leg.visibleDepthGbp, "GBP")}</td>
                  <td>{money(leg.recommendedStakeNative, leg.currency)}</td>
                  <td>{quoteAge(leg.quoteAgeMs)}</td>
                  <td>{leg.bookLevelsRequired}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel pa-pools-panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Automated pools vs required capital</div>
            <div className="panel-meta">GBP and USD standing pools are shown separately and are never mixed</div>
          </div>
        </div>
        <div className="pa-pool-grid pa-pool-grid-pad">
          {alert.autoPools.map((pool) => {
            const required = alert.legs
              .filter((leg) => leg.venue === pool.venue && leg.currency === pool.currency)
              .reduce((sum, leg) => sum + leg.recommendedStakeNative, 0);
            const extra = Math.max(0, required - pool.autoPoolNative);
            return (
              <article key={`${pool.venue}-${pool.currency}`} className="pa-pool-card">
                <div className="pa-pool-venue">
                  {venueLabel(pool.venue)} / {pool.currency} / AUTO_POOL
                </div>
                <div className="pa-pool-line">Standing pool {money(pool.autoPoolNative, pool.currency)}</div>
                <div className="pa-pool-line">Required at recommended {money(required, pool.currency)}</div>
                <div className="pa-pool-line pa-warning">
                  Additional manual {money(extra, pool.currency)}
                </div>
              </article>
            );
          })}
        </div>
      </section>

      <ManualOverrideTicket alert={alert} />
      <ExternalLegWorkflow alert={alert} />
    </div>
  );
}
