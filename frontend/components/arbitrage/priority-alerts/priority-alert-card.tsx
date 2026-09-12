import Link from "next/link";

import { money, percent, quoteAge, severityTone } from "../../../lib/priority-alerts/format";
import type { PriorityAlert } from "../../../lib/priority-alerts/types";

export function PriorityAlertCard({ alert }: { alert: PriorityAlert }) {
  const tone = severityTone(alert.severity);
  const limiting = alert.legs.find((leg) => leg.isLimiting);

  return (
    <Link className={`pa-card pa-card-${tone}`} href={`/arbitrage/priority-alerts/${alert.alertId}`}>
      <div className="pa-card-top">
        <span className={`pa-severity pa-severity-${tone}`}>{alert.severity.replaceAll("_", " ")}</span>
        <span className="pa-chip pa-chip-paper">PAPER MODE</span>
        <span className="pa-chip pa-chip-demo">DEMO/FIXTURE DATA</span>
      </div>
      <div className="pa-card-event">
        {alert.event.homeTeam} v {alert.event.awayTeam}
      </div>
      <div className="pa-card-market">
        {alert.event.competition} · {alert.market.label} · {alert.market.settlement}
      </div>
      <p className="pa-card-why">{alert.whyExceptional}</p>
      <dl className="pa-card-metrics">
        <div>
          <dt>Net edge</dt>
          <dd className="pa-accent">{percent(alert.netGuaranteedEdge)}</dd>
        </div>
        <div>
          <dt>Recommended</dt>
          <dd>{money(alert.recommendedSizeGbp, "GBP")}</dd>
        </div>
        <div>
          <dt>Guaranteed profit</dt>
          <dd className="pa-accent">{money(alert.expectedProfitGbpAtRecommended, "GBP")}</dd>
        </div>
        <div>
          <dt>Limiting leg</dt>
          <dd>
            {limiting ? `${limiting.venue} ${limiting.selectionLabel}` : "—"} · {money(alert.rawLimitingDepthGbp, "GBP")}
          </dd>
        </div>
        <div>
          <dt>Fill confidence</dt>
          <dd>{alert.fillConfidence}</dd>
        </div>
        <div>
          <dt>Quote age</dt>
          <dd>{quoteAge(alert.quoteAgeMs)}</dd>
        </div>
      </dl>
    </Link>
  );
}
