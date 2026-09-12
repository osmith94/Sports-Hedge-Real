"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  COMPETITION_OPTIONS,
  METRIC_OPTIONS,
  SCENARIO_DATA_LABEL,
  WINDOW_OPTIONS,
  defaultFilters,
  filterRanking,
  formatPct,
  formatSigned,
  getScenario,
  getTeam,
  managerSplit,
  matchesFor,
  scenarios,
  teams,
  timeResponseCurve,
  type ManagerEraFilter,
  type MetricId,
  type RankingRow,
  type ResponseWindow,
  type ScenarioFilters,
  type SeasonPhaseFilter,
  type VenueFilter,
} from "../../lib/scenario-fixtures";
import { TimeResponseCurve } from "./time-response-curve";
import styles from "./scenario-lab.module.css";
import {
  canonicalResponseWindow,
  canonicalScenarioId,
  canonicalTeamId,
  scenarioLabScenarioId,
  scenarioLabWindow,
} from "../../lib/demo/ids";
import { valueForScenario } from "../../lib/demo/value-signals";
import { ValueOverlay } from "../research/value-overlay";

function qualityClass(quality: RankingRow["dataQuality"]): string {
  if (quality === "Mixed regime") return styles.badgeWarn;
  if (quality === "Thin") return styles.badgeThin;
  return styles.badge;
}

function signedClass(value: number): string {
  if (value > 0.04) return styles.positive;
  if (value < -0.04) return styles.negative;
  return "";
}

export function ScenarioLabClient({ initialScenarioId }: { initialScenarioId?: string }) {
  const search = useSearchParams();
  const rawWindowParam = search.get("window");
  const mappedWindow = scenarioLabWindow(canonicalResponseWindow(rawWindowParam));
  const unknownWindowParam = Boolean(rawWindowParam) && mappedWindow === undefined;
  const [filters, setFilters] = useState<ScenarioFilters>(() => {
    const metric = (search.get("metric") as MetricId | null) ?? defaultFilters.metric;
    const team = canonicalTeamId(search.get("team"));
    return {
      ...defaultFilters,
      scenarioId: scenarioLabScenarioId(
        canonicalScenarioId(search.get("scenario") ?? initialScenarioId ?? defaultFilters.scenarioId),
      ),
      metric: METRIC_OPTIONS.some((item) => item.id === metric) ? metric : defaultFilters.metric,
      window: mappedWindow && WINDOW_OPTIONS.some((item) => item.id === mappedWindow) ? mappedWindow : defaultFilters.window,
      teamId: team && teams.some((item) => item.teamId === team) ? team : "all",
    };
  });
  const [focusTeamId, setFocusTeamId] = useState(() => canonicalTeamId(search.get("team")) || "arsenal");

  const scenario = getScenario(filters.scenarioId);
  const rows = useMemo(() => filterRanking(filters), [filters]);
  const focusTeam = getTeam(focusTeamId) ?? getTeam(rows[0]?.teamId ?? "arsenal");
  const focusRow = rows.find((row) => row.teamId === focusTeam?.teamId) ?? rows[0];
  const curve = focusTeam ? timeResponseCurve(filters, focusTeam.teamId) : [];
  const splits = focusTeam ? managerSplit(filters, focusTeam.teamId) : [];
  const matches = focusTeam ? matchesFor(filters, focusTeam.teamId) : [];
  const leagueEffect = focusRow?.leagueEffect ?? 0;
  const mixed = Boolean(focusRow?.regimeMix) || filters.managerEra === "all";
  const valueSignal = valueForScenario({
    teamId: focusTeamId,
    scenarioId: canonicalScenarioId(filters.scenarioId),
  });

  function update<K extends keyof ScenarioFilters>(key: K, value: ScenarioFilters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
  }

  return (
    <div className={styles.page}>
      <div className={styles.heading}>
        <div>
          <div className="eyebrow">Scenario Lab</div>
          <h1>Scenario response matrix</h1>
          <p className="page-subtitle">
            Rank teams by excess response to a structured football situation versus the league benchmark.
            Analysis only — no betting execution.
          </p>
        </div>
        <div className={styles.disclaimer}>
          <div className="demo-label">{SCENARIO_DATA_LABEL}</div>
          <div className={styles.note}>Illustrative SRC values for product review. Not historical findings.</div>
        </div>
      </div>

      {unknownWindowParam ? (
        <p className="page-subtitle">
          Unknown response window in the URL (`{rawWindowParam}`). It was not mapped to 0-15. Showing the lab default
          window instead.
        </p>
      ) : null}
      {valueSignal ? <ValueOverlay signal={valueSignal} /> : (
        <p className="page-subtitle">SRC ranking below is not a value claim. Open Research Home for odds-weighted EV.</p>
      )}

      <section className={styles.filters}>
        <div className={styles.filterGrid}>
          <label className={styles.field}>
            <span>Scenario</span>
            <select value={filters.scenarioId} onChange={(event) => update("scenarioId", event.target.value)}>
              {scenarios.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.field}>
            <span>Metric</span>
            <select value={filters.metric} onChange={(event) => update("metric", event.target.value as MetricId)}>
              {METRIC_OPTIONS.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.field}>
            <span>Response window</span>
            <select value={filters.window} onChange={(event) => update("window", event.target.value as ResponseWindow)}>
              {WINDOW_OPTIONS.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.field}>
            <span>Competition</span>
            <select value={filters.competition} onChange={(event) => update("competition", event.target.value)}>
              {COMPETITION_OPTIONS.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.field}>
            <span>Team</span>
            <select value={filters.teamId} onChange={(event) => update("teamId", event.target.value)}>
              <option value="all">All teams</option>
              {teams.map((team) => (
                <option key={team.teamId} value={team.teamId}>
                  {team.name}
                </option>
              ))}
            </select>
          </label>
          <label className={styles.field}>
            <span>Home / away</span>
            <select value={filters.venue} onChange={(event) => update("venue", event.target.value as VenueFilter)}>
              <option value="all">All venues</option>
              <option value="home">Home</option>
              <option value="away">Away</option>
            </select>
          </label>
          <label className={styles.field}>
            <span>Manager era</span>
            <select value={filters.managerEra} onChange={(event) => update("managerEra", event.target.value as ManagerEraFilter)}>
              <option value="current">Current regime only</option>
              <option value="previous">Previous manager</option>
              <option value="all">Include previous regimes</option>
            </select>
          </label>
          <label className={styles.field}>
            <span>Season phase</span>
            <select value={filters.seasonPhase} onChange={(event) => update("seasonPhase", event.target.value as SeasonPhaseFilter)}>
              <option value="all">All phases</option>
              <option value="OPENING_10">Opening 10</option>
              <option value="MID_SEASON">Mid-season</option>
              <option value="RUN_IN">Run-in</option>
            </select>
          </label>
          <label className={styles.field}>
            <span>Minimum sample N</span>
            <select value={String(filters.minSample)} onChange={(event) => update("minSample", Number(event.target.value))}>
              <option value="0">No floor</option>
              <option value="8">N ≥ 8</option>
              <option value="10">N ≥ 10</option>
              <option value="20">N ≥ 20</option>
            </select>
          </label>
          <label className={styles.field}>
            <span>Minimum confidence</span>
            <select
              value={String(filters.minConfidence)}
              onChange={(event) => update("minConfidence", Number(event.target.value))}
            >
              <option value="0">No floor</option>
              <option value="0.5">≥ 50%</option>
              <option value="0.7">≥ 70%</option>
              <option value="0.8">≥ 80%</option>
            </select>
          </label>
        </div>
        <div className={styles.scenarioStrip}>
          {scenarios.map((item) => (
            <Link
              key={item.id}
              href={`/scenario-lab/${item.id}`}
              className={`${styles.scenarioChip} ${item.id === filters.scenarioId ? styles.scenarioChipActive : ""}`}
            >
              {item.label}
            </Link>
          ))}
        </div>
      </section>

      <section className={styles.metrics}>
        <div className={styles.metricCard}>
          <div className={styles.metricLabel}>Teams in view</div>
          <div className={styles.metricValue}>{rows.length}</div>
          <div className={styles.metricFoot}>After sample and confidence floors</div>
        </div>
        <div className={styles.metricCard}>
          <div className={styles.metricLabel}>Top SRC</div>
          <div className={`${styles.metricValue} ${styles.positive}`}>{rows[0] ? formatSigned(rows[0].src) : "—"}</div>
          <div className={styles.metricFoot}>{rows[0] ? `${rows[0].teamName} · fixture coefficient` : "No rows"}</div>
        </div>
        <div className={styles.metricCard}>
          <div className={styles.metricLabel}>League effect</div>
          <div className={styles.metricValue}>{formatSigned(leagueEffect)}</div>
          <div className={styles.metricFoot}>
            {METRIC_OPTIONS.find((item) => item.id === filters.metric)?.label} · {WINDOW_OPTIONS.find((item) => item.id === filters.window)?.label}
          </div>
        </div>
        <div className={styles.metricCard}>
          <div className={styles.metricLabel}>Selected excess</div>
          <div className={`${styles.metricValue} ${signedClass(focusRow?.excessResponse ?? 0)}`}>
            {focusRow ? formatSigned(focusRow.excessResponse) : "—"}
          </div>
          <div className={styles.metricFoot}>{focusTeam?.name ?? "Select a team"} versus league</div>
        </div>
      </section>

      <section className={styles.layout}>
        <div className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <div className={styles.panelTitle}>{scenario.label}</div>
              <div className={styles.panelMeta}>
                {scenario.trigger}. Click a team name to open `/teams/[teamId]`. Selecting a row focuses the drill-down.
                All figures are {SCENARIO_DATA_LABEL}.
              </div>
            </div>
            <span className={styles.badge}>N={rows.reduce((sum, row) => sum + row.sampleN, 0)}</span>
          </div>
          {rows.length === 0 ? (
            <div className={styles.drillBody}>
              <div className={styles.empty}>No fixture rows pass the current sample/confidence floor. Lower the thresholds to inspect thin-sample teams.</div>
            </div>
          ) : (
            <div className={styles.tableWrap}>
              <table className={`${styles.table} ${styles.wideTable}`}>
                <thead>
                  <tr>
                    <th>Team</th>
                    <th>SRC</th>
                    <th>SRI 0–100</th>
                    <th>Team effect</th>
                    <th>League</th>
                    <th>Excess</th>
                    <th>Sample N</th>
                    <th>Confidence</th>
                    <th>Stability / quality</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr
                      key={row.teamId}
                      className={row.teamId === focusTeam?.teamId ? styles.rowActive : undefined}
                      onClick={() => setFocusTeamId(row.teamId)}
                    >
                      <td>
                        <Link className={styles.teamLink} href={`/teams/${row.teamId}`} onClick={(event) => event.stopPropagation()}>
                          {row.teamName}
                        </Link>
                      </td>
                      <td className={signedClass(row.src)}>{formatSigned(row.src)}</td>
                      <td>
                        <div className={styles.sri}>
                          <div className={styles.sriTrack}>
                            <div className={styles.sriFill} style={{ width: `${row.sri}%` }} />
                          </div>
                          {row.sri}
                        </div>
                      </td>
                      <td className={signedClass(row.teamEffect)}>{formatSigned(row.teamEffect)}</td>
                      <td>{formatSigned(row.leagueEffect)}</td>
                      <td className={signedClass(row.excessResponse)}>{formatSigned(row.excessResponse)}</td>
                      <td>{row.sampleN}</td>
                      <td>{formatPct(row.confidence)}</td>
                      <td>
                        <span className={qualityClass(row.dataQuality)}>
                          {row.stability} · {row.dataQuality}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className={styles.panel}>
          <div className={styles.panelHeader}>
            <div>
              <div className={styles.panelTitle}>{focusTeam?.name ?? "Scenario detail"}</div>
              <div className={styles.panelMeta}>
                {scenario.description} Manager: {focusTeam?.currentManager}. {SCENARIO_DATA_LABEL}.
              </div>
            </div>
            <Link href={`/scenario-lab/${filters.scenarioId}`} className={styles.badge}>
              Scenario detail
            </Link>
          </div>
          <div className={styles.drillBody}>
            {mixed && focusRow ? (
              <div className={styles.warning}>
                <strong>⚠ Regime mix</strong>
                This sample can contain matches from multiple managerial eras. Current-manager sample: N={focusRow.currentManagerN}. Full-team sample: N={focusRow.fullTeamN}. Compare current regime only rather than blending versions of the club.
              </div>
            ) : null}

            <div>
              <div className={styles.panelTitle}>Time-response windows</div>
              <div className={styles.legend} style={{ margin: "6px 0 8px" }}>
                <span>
                  <i className={styles.swatch} /> {focusTeam?.short ?? "Team"}
                </span>
                <span>
                  <i className={`${styles.swatch} ${styles.swatchLeague}`} /> League
                </span>
              </div>
              <div className={styles.windows}>
                {curve.map((point) => (
                  <div className={styles.windowCard} key={point.window}>
                    <div className={styles.windowLabel}>{point.label}</div>
                    <div className={`${styles.windowValue} ${signedClass(point.teamEffect)}`}>{formatSigned(point.teamEffect)}</div>
                    <div className={styles.windowLeague}>lg {formatSigned(point.leagueEffect)}</div>
                  </div>
                ))}
              </div>
            </div>

            {curve.length > 0 ? <TimeResponseCurve points={curve} teamLabel={focusTeam?.name ?? "Team"} /> : null}

            <div className={styles.split}>
              {splits.map((split) => (
                <div className={styles.splitCard} key={split.era}>
                  <div className={styles.splitTitle}>{split.era === "current" ? "Current manager" : "Previous manager"}</div>
                  <div className={styles.splitMeta}>
                    {split.manager} · N={split.sampleN}
                  </div>
                  <div className={styles.splitStats}>
                    <div className={styles.splitStat}>
                      <span>Effect</span>
                      <b className={signedClass(split.teamEffect)}>{formatSigned(split.teamEffect)}</b>
                    </div>
                    <div className={styles.splitStat}>
                      <span>Excess</span>
                      <b className={signedClass(split.excessResponse)}>{formatSigned(split.excessResponse)}</b>
                    </div>
                    <div className={styles.splitStat}>
                      <span>SRC</span>
                      <b className={signedClass(split.src)}>{formatSigned(split.src)}</b>
                    </div>
                  </div>
                </div>
              ))}
            </div>

            <div>
              <div className={styles.panelTitle}>Historical match list</div>
              <div className={styles.panelMeta} style={{ marginBottom: 8 }}>
                Fixture rows only. Metric shown is the demo {METRIC_OPTIONS.find((item) => item.id === filters.metric)?.label.toLowerCase()} count in the selected-style window.
              </div>
              {matches.length === 0 ? (
                <div className={styles.empty}>No fixture match rows for this team/scenario/filter combination.</div>
              ) : (
                <div className={styles.tableWrap}>
                  <table className={styles.table}>
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Fixture</th>
                        <th>Min</th>
                        <th>Score</th>
                        <th>Value</th>
                        <th>Manager</th>
                        <th>Phase</th>
                      </tr>
                    </thead>
                    <tbody>
                      {matches.map((match) => (
                        <tr key={match.matchId}>
                          <td>{match.date}</td>
                          <td>{match.fixture}</td>
                          <td>{match.minute}</td>
                          <td>{match.scoreAtTrigger}</td>
                          <td>{match.metricValue}</td>
                          <td>{match.manager}</td>
                          <td>{match.seasonPhase}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
