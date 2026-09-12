import { DiscoveredFixture, LiveRefreshStatus } from "../lib/api";
import {
  DISCOVERY_TABLE_HEADERS,
  arbClaimLabel,
  freshnessLabel,
  polymarketCoverageLabel,
} from "../lib/discovered-fixture-display";
import { percent, percentPoints, relativeTime } from "../lib/format";

function scoreText(item: DiscoveredFixture): string {
  if (item.live_score_supported && item.home_score != null && item.away_score != null) {
    return `${item.home_score}–${item.away_score} (Matchbook payload)`;
  }
  return "unavailable · Matchbook payload has no score fields";
}

function decimalText(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export function DiscoveredFixturesPanel({
  status,
  available,
}: {
  status: LiveRefreshStatus | null;
  available: boolean;
}) {
  if (!available || !status) {
    return (
      <div className="empty-live-compact">
        Discovery status unavailable. No fabricated fixtures.
      </div>
    );
  }

  const items = status.discovered_fixtures;

  return (
    <>
      <p className="section-copy">
        PL / Championship / La Liga only. Last collection{" "}
        {status.last_completed_at ? relativeTime(status.last_completed_at) : "never"}.
        {status.last_error ? ` Last error: ${status.last_error}` : ""}
      </p>
      {items.length === 0 ? (
        <div className="empty-live-compact">No in-scope Matchbook fixtures.</div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {DISCOVERY_TABLE_HEADERS.map((header) => (
                  <th key={header}>{header}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.source_event_id}>
                  <td className="row-title">
                    {item.home_team} v {item.away_team}
                    <div className="muted">
                      {item.competition}
                      {item.target_competition_code ? ` · ${item.target_competition_code}` : ""}
                    </div>
                  </td>
                  <td>{new Date(item.kickoff_utc).toISOString()}</td>
                  <td>
                    {item.fixture_status ?? "—"}
                    {item.in_running ? " · in-running" : ""}
                  </td>
                  <td>{polymarketCoverageLabel(item)}</td>
                  <td>{item.matched_market_count}</td>
                  <td className="muted">
                    {item.market_family ?? "—"}
                    {item.outcome_context ? ` · ${item.outcome_context}` : ""}
                  </td>
                  <td>{decimalText(item.best_matchbook_price)}</td>
                  <td>{decimalText(item.best_polymarket_price)}</td>
                  <td className={Number(item.current_net_edge) < 0 ? "edge-negative" : ""}>
                    {percent(item.current_net_edge)}
                  </td>
                  <td>{percent(item.trigger_net_edge)}</td>
                  <td>{percentPoints(item.distance_to_trigger_pp)}</td>
                  <td className="muted">{freshnessLabel(item)}</td>
                  <td className="muted">{item.no_comparison_reason ?? "backend comparison available"}</td>
                  <td>{arbClaimLabel(item)}</td>
                  <td className="muted">{scoreText(item)}</td>
                  <td>{relativeTime(item.last_seen_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
