"use client";

import Link from "next/link";
import { DiscoveredFixture, LiveRefreshStatus } from "../lib/api";
import {
  DISCOVERY_TABLE_HEADERS,
  equivalentCountLabel,
  fixtureHref,
  fixturePhaseLabel,
  freshnessLabel,
  opportunityStateLabel,
  technicalDetailLines,
  venuePresent,
} from "../lib/discovered-fixture-display";
import { kickoffLocalLabel, kickoffRelativeLabel, percent, percentPoints } from "../lib/format";

function VenueMark({
  code,
  present,
}: {
  code: string;
  present: boolean;
}) {
  return (
    <span className={present ? "venue-mark venue-mark-on" : "venue-mark"} title={present ? `${code} present` : `${code} not on this fixture`}>
      {code}
    </span>
  );
}

function FixtureRow({ item }: { item: DiscoveredFixture }) {
  const relative = kickoffRelativeLabel(item.kickoff_utc);
  const details = technicalDetailLines(item);
  return (
    <>
      <tr>
        <td className="row-title wrap">
          <Link className="fixture-link" href={fixtureHref(item)}>
            {item.home_team} v {item.away_team}
          </Link>
          <div className="muted">
            {item.competition}
            {item.target_competition_code ? ` · ${item.target_competition_code}` : ""}
          </div>
        </td>
        <td className="wrap">
          {kickoffLocalLabel(item.kickoff_utc)}
          {relative ? <div className="muted">{relative}</div> : null}
        </td>
        <td>{fixturePhaseLabel(item)}</td>
        <td>
          <span className="venue-marks">
            <VenueMark code="MB" present={venuePresent(item.matchbook_matched)} />
            <VenueMark code="PM" present={venuePresent(item.polymarket_matched)} />
            <VenueMark code="K" present={venuePresent(item.kalshi_matched)} />
          </span>
        </td>
        <td>{equivalentCountLabel(item)}</td>
        <td className={Number(item.current_net_edge) < 0 ? "edge-negative" : ""}>
          {percent(item.current_net_edge)}
          {item.distance_to_trigger_pp != null && item.distance_to_trigger_pp !== "" ? (
            <div className="muted">{percentPoints(item.distance_to_trigger_pp)} to trigger</div>
          ) : null}
        </td>
        <td>{opportunityStateLabel(item)}</td>
      </tr>
      <tr className="discovery-detail-row">
        <td colSpan={DISCOVERY_TABLE_HEADERS.length}>
          <details>
            <summary>Advanced · mapping and quotes</summary>
            <div className="muted wrap">{details.join(" · ")}</div>
            <div className="muted">{freshnessLabel(item)}</div>
          </details>
        </td>
      </tr>
    </>
  );
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
  const warnings = status.config_warnings ?? [];

  return (
    <>
      <p className="section-copy">
        {status.operator_summary
          ? status.operator_summary
          : `PL / Championship / La Liga. Last collection ${
              status.last_completed_at ? kickoffRelativeLabel(status.last_completed_at) ?? "just now" : "never"
            }.`}
        {status.last_error ? ` Last error: ${status.last_error}` : ""}
      </p>
      {warnings.length ? (
        <div className="scan-message scan-message-error" role="status">
          {warnings.join(" ")}
        </div>
      ) : null}
      {items.length === 0 ? (
        <div className="empty-live-compact">No in-scope fixtures yet.</div>
      ) : (
        <div className="table-wrap">
          <table className="discovery-compact">
            <thead>
              <tr>
                {DISCOVERY_TABLE_HEADERS.map((header) => (
                  <th key={header}>{header}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <FixtureRow item={item} key={item.canonical_event_id} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
