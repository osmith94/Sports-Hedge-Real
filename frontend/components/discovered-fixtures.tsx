import { DiscoveredFixture, LiveRefreshStatus } from "../lib/api";
import { relativeTime } from "../lib/format";

function scoreText(item: DiscoveredFixture): string {
  if (item.live_score_supported && item.home_score != null && item.away_score != null) {
    return `${item.home_score}–${item.away_score} (Matchbook payload)`;
  }
  return "unavailable · Matchbook payload has no score fields";
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
      <div className="empty-live">
        Live discovery status is unavailable. No fabricated fixtures or scores are substituted.
      </div>
    );
  }

  const items = status.discovered_fixtures;

  return (
    <>
      <p className="section-copy">
        Matchbook is the primary live fixture-discovery source. Polymarket is matched onto the same
        canonical event when settlement-equivalent. Live scores are shown only when Matchbook includes
        them; they are never invented. Last collection completed{" "}
        {status.last_completed_at ? relativeTime(status.last_completed_at) : "never"}.
        {status.last_error ? ` Last error: ${status.last_error}` : ""}
      </p>
      {items.length === 0 ? (
        <div className="empty-live">
          No Matchbook fixtures in the latest collection cycle. Empty live discovery stays empty.
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Fixture</th>
                <th>Kickoff</th>
                <th>Matchbook status</th>
                <th>Polymarket</th>
                <th>Score</th>
                <th>Last seen</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.source_event_id}>
                  <td className="row-title">
                    {item.home_team} v {item.away_team}
                    <div className="muted">{item.competition}</div>
                  </td>
                  <td>{new Date(item.kickoff_utc).toISOString()}</td>
                  <td>
                    {item.fixture_status ?? "—"}
                    {item.in_running ? " · in-running" : ""}
                  </td>
                  <td>{item.polymarket_matched ? "matched" : "unmatched"}</td>
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
