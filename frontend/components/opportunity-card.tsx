import { ArbitrageOpportunity } from "../lib/arbitrage-ops";
import { money, percent, percentPoints, relativeTime } from "../lib/format";

function provenanceLabel(value: ArbitrageOpportunity["provenance"]): string {
  return value === "LIVE_PAPER" ? "LIVE PAPER" : "DEMO / FIXTURE";
}

function statusClass(status: ArbitrageOpportunity["status"]): string {
  if (status === "TRIGGERED" || status === "FILLED" || status === "PAPER_FILLING" || status === "PARTIAL") return "ops-status is-hot";
  if (status === "WATCHING" || status === "APPROACHING") return "ops-status is-watch";
  if (status === "REJECTED" || status === "EXPIRED") return "ops-status is-reject";
  return "ops-status";
}

export function OpportunityCard({
  item,
  executable,
}: {
  item: ArbitrageOpportunity;
  executable?: boolean;
}) {
  return (
    <article className={`opp-card ${item.executable || executable ? "opp-card-hot" : ""}`}>
      <div className="opp-card-top">
        <div>
          <div className="opp-event">{item.eventLabel}</div>
          <div className="opp-market">
            {item.marketLabel}
            {item.competition ? ` · ${item.competition}` : ""}
          </div>
          <div className="opp-settlement">{item.settlement}</div>
        </div>
        <div className="opp-card-flags">
          <span className={item.provenance === "LIVE_PAPER" ? "status-badge" : "demo-chip"}>
            {provenanceLabel(item.provenance)}
          </span>
          <span className={statusClass(item.status)}>{item.status}</span>
        </div>
      </div>

      <div className="opp-metrics">
        <div>
          <div className="opp-k">Net arb</div>
          <div className={`opp-v ${item.executable ? "edge" : ""}`}>{percent(item.netArb)}</div>
        </div>
        <div>
          <div className="opp-k">Trigger</div>
          <div className="opp-v">{percent(item.trigger)}</div>
        </div>
        <div>
          <div className="opp-k">Distance</div>
          <div className="opp-v">{item.executable ? "triggered" : percentPoints(item.distanceToTriggerPp)}</div>
        </div>
        <div>
          <div className="opp-k">Capital</div>
          <div className="opp-v">{money(item.capitalRequiredGbp)}</div>
        </div>
        <div>
          <div className="opp-k">{executable ? "Guaranteed" : "Move"}</div>
          <div className="opp-v">
            {executable ? money(item.guaranteedProfitGbp) : item.movement ?? "—"}
          </div>
        </div>
      </div>

      <div className="opp-meta">
        <span>{item.venues.join(" / ")}</span>
        <span>{item.currencies.join(" · ")}</span>
        <span>lock {item.expectedLock ?? "—"}</span>
        <span>quotes {item.quoteFreshness ?? "—"}</span>
        <span>depth {item.executableDepth ?? "—"}</span>
        <span>limit {item.limitingLeg ?? "—"}</span>
        <span>risk {item.executionRisk ?? "—"}</span>
        <span>{relativeTime(item.scannedAt)}</span>
      </div>
      {item.riskFlags.length ? (
        <div className="opp-flags">{item.riskFlags.slice(0, 4).map((flag) => flag.replaceAll("_", " ")).join(" · ")}</div>
      ) : null}
      {!item.executable ? (
        <div className="opp-note">Not executable · has not passed settlement, payoff and cost gates.</div>
      ) : (
        <div className="opp-note">Paper-eligible · no venue orders will be placed.</div>
      )}
    </article>
  );
}
