"use client";

import Link from "next/link";
import { useState } from "react";
import { DiscoveredFixture, LiveRefreshStatus } from "../lib/api";
import {
  ALL_DISCOVERY_FILTER,
  applyFixtureDiscoveryFilters,
  competitionFilterOptions,
  sportFilterOptions,
} from "../lib/fixture-discovery-filters";
import { CONFIG_WARNING_BANNER_CLASS } from "../lib/config-warning-display";
import {
  BEST_ARB_MARKET_HELP,
  DISCOVERY_TABLE_HEADERS,
  EQUIVALENT_MARKETS_HELP,
  RISK_HELP,
  bestArbMarketLabel,
  edgeTone,
  equivalentCountLabel,
  fixtureHref,
  kickoffContextLines,
  lastRefreshLabel,
  marketEvaluationLabel,
  marketEvaluationUnevaluated,
  netEdgeSummary,
  riskLabel,
  riskReasonsLabel,
  signedEdgeVsTrigger,
  technicalDetailLines,
  venuePresent,
  venuePriceLabel,
} from "../lib/discovered-fixture-display";
import {
  coverageRowLabel,
  fixtureCoverageRows,
  universeArchetypeSummaryLines,
} from "../lib/catalogue-coverage-display";
import { kickoffLocalLabel } from "../lib/format";
import { dualScanStatusLines } from "../lib/scan-status-display";
import { useHydratedNowMs } from "./hydrated-relative-time";

function HelpMark({ label, text }: { label: string; text: string }) {
  return (
    <span className="col-help" title={text} aria-label={label}>
      i
    </span>
  );
}

function VenueCell({
  code,
  present,
  price,
}: {
  code: string;
  present: boolean;
  price: string | number | null | undefined;
}) {
  return (
    <div className="venue-cell">
      <span
        className={present ? "venue-mark venue-mark-on" : "venue-mark"}
        title={present ? `${code} fixture found` : `${code} not on this fixture`}
      >
        {code}
      </span>
      <div className="muted">{venuePriceLabel(price, present)}</div>
    </div>
  );
}

function FixtureRow({ item, nowMs }: { item: DiscoveredFixture; nowMs: number | null }) {
  const details = technicalDetailLines(item);
  const tone = edgeTone(item);
  const context = kickoffContextLines(item, nowMs);
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
          {kickoffLocalLabel(item.kickoff_utc, nowMs)}
          {context.map((line) => (
            <div className="muted" key={line}>
              {line}
            </div>
          ))}
        </td>
        <td>
          <VenueCell
            code="MB"
            present={venuePresent(item.matchbook_matched)}
            price={item.best_matchbook_price}
          />
        </td>
        <td>
          <VenueCell
            code="PM"
            present={venuePresent(item.polymarket_matched)}
            price={item.best_polymarket_price}
          />
        </td>
        <td>
          <VenueCell
            code="K"
            present={venuePresent(item.kalshi_matched)}
            price={item.best_kalshi_price}
          />
        </td>
        <td>
          {marketEvaluationUnevaluated(item) ? (
            <span title={marketEvaluationLabel(item)}>
              {equivalentCountLabel(item)}
              <div className="muted">{marketEvaluationLabel(item)}</div>
            </span>
          ) : (
            <span title={EQUIVALENT_MARKETS_HELP}>{equivalentCountLabel(item)}</span>
          )}
        </td>
        <td className="wrap">
          {bestArbMarketLabel(item)}
        </td>
        <td className={Number(item.current_net_edge) < 0 ? "edge-negative" : ""}>
          {netEdgeSummary(item)}
        </td>
        <td className={tone === "qualifying" ? "edge-qualifying" : tone === "near" ? "edge-near" : ""}>
          {signedEdgeVsTrigger(item)}
        </td>
        <td
          className={
            item.execution_risk_band === "low"
              ? "risk-low"
              : item.execution_risk_band === "high" || item.execution_risk_band === "extreme"
                ? "risk-high"
                : item.execution_risk_score != null
                  ? "risk-medium"
                  : ""
          }
          title={riskReasonsLabel(item)}
        >
          {riskLabel(item)}
        </td>
        <td>{lastRefreshLabel(item, nowMs)}</td>
      </tr>
      <tr className="discovery-detail-row">
        <td colSpan={DISCOVERY_TABLE_HEADERS.length}>
          <details>
            <summary>Advanced · mapping, quotes, and all market comparisons</summary>
            <div className="muted wrap">{details.join(" · ")}</div>
            {fixtureCoverageRows(item).length ? (
              <ul className="catalogue-coverage-list">
                {fixtureCoverageRows(item).map((row) => (
                  <li key={row.archetype}>{coverageRowLabel(row)}</li>
                ))}
              </ul>
            ) : null}
            <div className="muted">
              Expand this fixture for every equivalent market. The headline is the best
              executable opportunity only.
            </div>
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
  const nowMs = useHydratedNowMs();
  const [sport, setSport] = useState(ALL_DISCOVERY_FILTER);
  const [competition, setCompetition] = useState(ALL_DISCOVERY_FILTER);
  if (!available || !status) {
    return (
      <div className="empty-live-compact">
        Discovery status unavailable. No fabricated fixtures.
      </div>
    );
  }

  const items = status.discovered_fixtures;
  const sportOptions = sportFilterOptions(items);
  const sportActive =
    sport !== ALL_DISCOVERY_FILTER && sportOptions.some((option) => option.token === sport)
      ? sport
      : ALL_DISCOVERY_FILTER;
  const competitionOptions = competitionFilterOptions(items, sportActive);
  const competitionActive =
    competition !== ALL_DISCOVERY_FILTER &&
    competitionOptions.some((option) => option.key === competition)
      ? competition
      : ALL_DISCOVERY_FILTER;
  const visible = applyFixtureDiscoveryFilters(items, {
    sport: sportActive,
    competition: competitionActive,
  });
  const warnings = status.config_warnings ?? [];
  const snapshot = status.operator_summary
    ? `Stamped scan snapshot (not live age) · ${status.operator_summary}`
    : `Target football competitions only. ${dualScanStatusLines(status, nowMs).join(" · ")}`;
  const archetypeLines = universeArchetypeSummaryLines(status);

  return (
    <>
      <p className="section-copy">
        {snapshot}
        {status.last_error ? ` Last error: ${status.last_error}` : ""}
      </p>
      {archetypeLines.length ? (
        <p className="section-copy catalogue-coverage-summary">
          Coverage by archetype: {archetypeLines.join(" · ")}
        </p>
      ) : null}
      {warnings.length ? (
        <div className={CONFIG_WARNING_BANNER_CLASS} role="status">
          {warnings.join(" ")}
        </div>
      ) : null}
      {items.length === 0 ? (
        <div className="empty-live-compact">
          {status.last_error
            ? "Last scan finished with an error. No in-scope fixtures were stored."
            : status.discovered_fixtures.length === 0
              ? "No in-scope fixtures in current inventory."
              : "No in-scope fixtures yet."}
        </div>
      ) : (
        <>
          <div className="discovery-filter-bar" role="group" aria-label="Fixture discovery display filters">
            <p className="muted discovery-filter-note">Display only. Does not change scanner scope.</p>
            <div className="discovery-filter-row">
              <span className="discovery-filter-label">Sport</span>
              <button
                type="button"
                className={
                  sportActive === ALL_DISCOVERY_FILTER
                    ? "discovery-filter-chip on"
                    : "discovery-filter-chip"
                }
                aria-pressed={sportActive === ALL_DISCOVERY_FILTER}
                onClick={() => {
                  setSport(ALL_DISCOVERY_FILTER);
                  setCompetition(ALL_DISCOVERY_FILTER);
                }}
              >
                All {items.length}
              </button>
              {sportOptions.map((option) => (
                <button
                  type="button"
                  key={option.token}
                  className={
                    sportActive === option.token ? "discovery-filter-chip on" : "discovery-filter-chip"
                  }
                  aria-pressed={sportActive === option.token}
                  onClick={() => {
                    setSport(option.token);
                    setCompetition(ALL_DISCOVERY_FILTER);
                  }}
                >
                  {option.label} {option.count}
                </button>
              ))}
            </div>
            <label className="discovery-filter-row">
              <span className="discovery-filter-label">Competition / Event</span>
              <select
                value={competitionActive}
                onChange={(event) => setCompetition(event.target.value)}
              >
                <option value={ALL_DISCOVERY_FILTER}>
                  All {sportActive === ALL_DISCOVERY_FILTER ? items.length : competitionOptions.reduce((sum, option) => sum + option.count, 0)}
                </option>
                {competitionOptions.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.label} {option.count}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {visible.length === 0 ? (
            <div className="empty-live-compact">No fixtures match this display filter.</div>
          ) : (
        <div className="table-wrap">
          <table className="discovery-compact">
            <thead>
              <tr>
                {DISCOVERY_TABLE_HEADERS.map((header) => (
                  <th key={header}>
                    {header}
                    {header === "Equivalent" ? (
                      <HelpMark label="What equivalent markets means" text={EQUIVALENT_MARKETS_HELP} />
                    ) : null}
                    {header === "Best arb market" ? (
                      <HelpMark label="What best arb market means" text={BEST_ARB_MARKET_HELP} />
                    ) : null}
                    {header === "Risk" ? (
                      <HelpMark label="What risk score means" text={RISK_HELP} />
                    ) : null}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visible.map((item) => (
                <FixtureRow item={item} key={item.canonical_event_id} nowMs={nowMs} />
              ))}
            </tbody>
          </table>
        </div>
          )}
        </>
      )}
    </>
  );
}
