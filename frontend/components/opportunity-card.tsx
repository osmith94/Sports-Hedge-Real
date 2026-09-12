import { ArbitrageOpportunity } from "../lib/arbitrage-ops";
import { grossPricesEffectivelyEqual } from "../lib/comfort-threshold";
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

function operatorNote(item: ArbitrageOpportunity, executable?: boolean): string {
  if (item.executable || executable) {
    return "Paper-eligible validated complete-set opportunity. Guaranteed profit is solver-owned. No venue orders will be placed.";
  }
  if (item.status === "WATCHING" || item.status === "APPROACHING") {
    return "Validated watch candidate. Semantics, costs, FX, depth, freshness and risk gates have already passed. It is below the backend trigger and is not guaranteed arbitrage until the strict trigger/solver condition is met.";
  }
  if (item.status === "REJECTED") {
    return "Not a near-arb. Non-economic gates failed; this row is not reclassified in the browser.";
  }
  return "Not executable paper arbitrage.";
}

export function OpportunityCard({
  item,
  executable,
}: {
  item: ArbitrageOpportunity;
  executable?: boolean;
}) {
  const belowEven = item.netArb !== null && item.netArb < 0;
  const feeNote =
    belowEven && grossPricesEffectivelyEqual(item.grossArb)
      ? "Gross prices are effectively equal; fees/costs are why net margin is below break-even."
      : null;

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
          <div className="opp-k">Net margin</div>
          <div className={`opp-v ${item.executable ? "edge" : ""} ${belowEven ? "edge-negative" : ""}`}>
            {percent(item.netArb)}
            {belowEven ? " · below break-even" : ""}
          </div>
        </div>
        <div>
          <div className="opp-k">Backend trigger</div>
          <div className="opp-v">{percent(item.trigger)}</div>
        </div>
        <div>
          <div className="opp-k">Distance to trigger</div>
          <div className="opp-v">{item.executable ? "triggered" : percentPoints(item.distanceToTriggerPp)}</div>
        </div>
        <div>
          <div className="opp-k">Capital</div>
          <div className="opp-v">{money(item.capitalRequiredGbp)}</div>
        </div>
        <div>
          <div className="opp-k">{executable ? "Guaranteed" : "Gross"}</div>
          <div className="opp-v">
            {executable ? money(item.guaranteedProfitGbp) : percent(item.grossArb)}
          </div>
        </div>
      </div>

      <div className="opp-meta">
        <span>{item.venues.join(" / ")}</span>
        <span>source {item.discoverySource ?? "matchbook"}</span>
        <span>{item.currencies.join(" · ")}</span>
        <span>lock {item.expectedLock ?? "—"}</span>
        <span>age at last evaluation {item.quoteFreshness ?? "—"}</span>
        <span>updated {relativeTime(item.scannedAt)}</span>
        <span>
          narrative{" "}
          {item.strikeNarrative === "approaching"
            ? "approaching threshold"
            : item.strikeNarrative === "moving_away"
              ? "moving away"
              : item.strikeNarrative === "stable"
                ? "stable vs prior observation"
                : "insufficient history"}
        </span>
        <span>score {item.liveScoreLabel ?? "unavailable"}</span>
        <span>depth {item.executableDepth ?? "—"}</span>
        <span>limit {item.limitingLeg ?? "—"}</span>
        <span>risk {item.executionRisk ?? "—"}</span>
      </div>
      {item.riskFlags.length ? (
        <div className="opp-flags">{item.riskFlags.slice(0, 4).map((flag) => flag.replaceAll("_", " ")).join(" · ")}</div>
      ) : null}
      <div className="opp-note">{operatorNote(item, executable)}</div>
      {feeNote ? <div className="opp-note">{feeNote}</div> : null}
    </article>
  );
}
