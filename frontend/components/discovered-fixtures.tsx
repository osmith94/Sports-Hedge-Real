"use client";

import Link from "next/link";
import { useState } from "react";
import { DiscoveredFixture, UniverseCatalogueFixture } from "../lib/api";
import { CONFIG_WARNING_BANNER_CLASS } from "../lib/config-warning-display";
import {
  ALL_DISCOVERY_FILTER,
  applyFixtureDiscoveryFilters,
  competitionFilterOptions,
  sportFilterOptions,
} from "../lib/fixture-discovery-filters";
import { fixtureDetailHref } from "../lib/discovered-fixture-display";
import { kickoffLocalLabel } from "../lib/format";
import { useHydratedNowMs } from "./hydrated-relative-time";

const CATALOGUE_HEADERS = ["Fixture", "Kickoff", "Venues", "Status", "Updated"] as const;

type CatalogueRow = UniverseCatalogueFixture | DiscoveredFixture;

function venueMarks(item: CatalogueRow): string {
  return [
    item.matchbook_matched ? "MB" : "—",
    item.polymarket_matched ? "PM" : "—",
    item.kalshi_matched ? "K" : "—",
  ].join(" / ");
}

export function DiscoveredFixturesPanel({
  fixtures,
  available,
  configWarnings = [],
  lastError = null,
}: {
  fixtures: CatalogueRow[];
  available: boolean;
  configWarnings?: string[];
  lastError?: string | null;
}) {
  const nowMs = useHydratedNowMs();
  const [sport, setSport] = useState(ALL_DISCOVERY_FILTER);
  const [competition, setCompetition] = useState(ALL_DISCOVERY_FILTER);
  if (!available) {
    return (
      <div className="empty-live-compact">
        Universe catalogue unavailable. No fabricated fixtures.
      </div>
    );
  }

  const items = fixtures;
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

  return (
    <>
      <p className="section-copy">
        Universe catalogue of events UNIVERSE has published. Recognition only. No live prices,
        edges, or quote ages. HOT and BACKGROUND pricing do not change this list.
        {lastError ? ` Last error: ${lastError}` : ""}
      </p>
      {configWarnings.length ? (
        <div className={CONFIG_WARNING_BANNER_CLASS} role="status">
          {configWarnings.join(" ")}
        </div>
      ) : null}
      {items.length === 0 ? (
        <div className="empty-live-compact">No UNIVERSE catalogue fixtures yet.</div>
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
                  All{" "}
                  {sportActive === ALL_DISCOVERY_FILTER
                    ? items.length
                    : competitionOptions.reduce((sum, option) => sum + option.count, 0)}
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
                    {CATALOGUE_HEADERS.map((header) => (
                      <th key={header}>{header}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {visible.map((item) => (
                    <tr key={item.canonical_event_id}>
                      <td className="row-title wrap">
                        <Link className="fixture-link" href={fixtureDetailHref(item.canonical_event_id)}>
                          {item.home_team} v {item.away_team}
                        </Link>
                        <div className="muted">
                          {item.competition}
                          {item.target_competition_code ? ` · ${item.target_competition_code}` : ""}
                        </div>
                      </td>
                      <td>{kickoffLocalLabel(item.kickoff_utc, nowMs)}</td>
                      <td>{venueMarks(item)}</td>
                      <td>{item.fixture_status || "—"}</td>
                      <td>{"updated_at" in item && item.updated_at ? item.updated_at : "—"}</td>
                    </tr>
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
