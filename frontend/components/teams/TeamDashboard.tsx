"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import styles from "../../app/teams/teams.module.css";
import {
  formatPct,
  formatSigned,
  getCompetition,
  metricLabel,
  phaseLabel,
  PROFILE_WINDOWS,
  qualityLabel,
  scenarioLabHref,
  strongestResponses,
  weakestResponses,
  type ProfileWindow,
  type TeamDashboard,
} from "../../lib/team-fixtures";
import { DemoDataBanner } from "./DemoDataBanner";
import { ScenarioResponseCard } from "./ScenarioResponseCard";
import { valueForScenario } from "../../lib/demo/value-signals";
import { ValueOverlay } from "../research/value-overlay";

function qualityClass(quality: TeamDashboard["team"]["dataQuality"]): string {
  if (quality === "sufficient") return styles.qualitySufficient;
  if (quality === "limited") return styles.qualityLimited;
  return styles.qualitySparse;
}

export function TeamDashboardView({ dashboard }: { dashboard: TeamDashboard }) {
  const [windowId, setWindowId] = useState<ProfileWindow>("current_manager");
  const competition = getCompetition(dashboard.team.competitionId);
  const fixtureCompetition = getCompetition(dashboard.nextFixture.competitionId);
  const rows = dashboard.profiles[windowId];
  const strongest = useMemo(() => strongestResponses(rows, 4), [rows]);
  const weakest = useMemo(() => weakestResponses(rows, 3), [rows]);

  return (
    <>
      <Link className={styles.backLink} href="/teams">
        ← Team Explorer
      </Link>

      <div className="page-heading">
        <div>
          <div className="eyebrow">Team dashboard</div>
          <h1>{dashboard.team.name}</h1>
          <p className="page-subtitle">
            Scenario Response Profile for the selected time window. SRC summarises excess response versus the league
            benchmark. This page is analysis-only and uses labelled fixture coefficients.
          </p>
        </div>
        <div className="demo-label">DEMO / FIXTURE DATA</div>
      </div>

      <DemoDataBanner />
      {dashboard.team.id === "arsenal" ? (
        <>
          {["favourite-concedes-first", "early-goal-scored"].map((scenarioId) => {
            const signal = valueForScenario({ teamId: "arsenal", scenarioId });
            return signal ? <ValueOverlay key={signal.signalId} signal={signal} /> : null;
          })}
        </>
      ) : null}

      <section className={styles.identity}>
        <div className={styles.identityCard}>
          <div className={styles.kicker}>{competition.name}</div>
          <h2 className={styles.clubTitle}>{dashboard.team.name}</h2>
          <p className={styles.clubSub}>
            {dashboard.team.city} · {dashboard.team.colours} · primary metric {metricLabel(dashboard.team.primaryMetric)}
          </p>
          <div className={styles.metricStrip}>
            <div className={styles.stat}>
              <div className={styles.statLabel}>SRC index</div>
              <div className={styles.statValue}>{dashboard.team.srcIndex}</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statLabel}>Manager</div>
              <div className={styles.statValue}>{dashboard.currentManager.managerName}</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statLabel}>Phase</div>
              <div className={styles.statValue}>{phaseLabel(dashboard.currentManager.phase)}</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statLabel}>Matches in regime</div>
              <div className={styles.statValue}>{dashboard.currentManager.matchesInRegime}</div>
            </div>
          </div>
        </div>

        <div className={styles.fixtureCard}>
          <div className={styles.kicker}>Next fixture</div>
          <div className={styles.kickoffTime}>{dashboard.nextFixture.kickoffLabel}</div>
          <div className={styles.fixtureLine}>
            <span>
              {dashboard.nextFixture.venue === "H" ? "Home" : "Away"} vs {dashboard.nextFixture.opponentName}
            </span>
            <span>{fixtureCompetition.name}</span>
          </div>
          <p className={styles.clubSub}>{dashboard.nextFixture.context}</p>
          <div className={styles.fixtureLine}>
            <span>
              Favourite lean: {dashboard.nextFixture.impliedFavourite === "even" ? "even" : dashboard.nextFixture.impliedFavourite}
            </span>
            <Link
              className={styles.labLink}
              href={scenarioLabHref({
                teamId: dashboard.team.id,
                scenarioId: strongest[0]?.scenarioId ?? "favourite-concedes-first",
                metric: strongest[0]?.metric ?? "corners",
                responseWindow: strongest[0]?.responseWindow ?? "0-15",
                profileWindow: windowId,
              })}
            >
              Scenario Lab →
            </Link>
          </div>
        </div>
      </section>

      <section className={styles.identity}>
        <div className={styles.managerCard}>
          <div className={styles.kicker}>Current manager era</div>
          <div className={styles.teamName}>{dashboard.currentManager.managerName}</div>
          <div className={styles.managerGrid}>
            <div className={styles.stat}>
              <div className={styles.statLabel}>Appointment</div>
              <div className={styles.statValue}>{dashboard.currentManager.appointmentType}</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statLabel}>Regime start</div>
              <div className={styles.statValue}>{dashboard.currentManager.regimeStart}</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statLabel}>Manager phase</div>
              <div className={styles.statValue}>{phaseLabel(dashboard.currentManager.phase)}</div>
            </div>
            <div className={styles.stat}>
              <div className={styles.statLabel}>Matches in regime</div>
              <div className={styles.statValue}>{dashboard.currentManager.matchesInRegime}</div>
            </div>
          </div>
        </div>
        <div className={styles.managerCard}>
          <div className={styles.kicker}>Previous regime</div>
          {dashboard.previousManager ? (
            <>
              <div className={styles.teamName}>{dashboard.previousManager.managerName}</div>
              <div className={styles.managerGrid}>
                <div className={styles.stat}>
                  <div className={styles.statLabel}>Appointment</div>
                  <div className={styles.statValue}>{dashboard.previousManager.appointmentType}</div>
                </div>
                <div className={styles.stat}>
                  <div className={styles.statLabel}>Regime start</div>
                  <div className={styles.statValue}>{dashboard.previousManager.regimeStart}</div>
                </div>
                <div className={styles.stat}>
                  <div className={styles.statLabel}>Phase at end</div>
                  <div className={styles.statValue}>{phaseLabel(dashboard.previousManager.phase)}</div>
                </div>
                <div className={styles.stat}>
                  <div className={styles.statLabel}>Matches in regime</div>
                  <div className={styles.statValue}>{dashboard.previousManager.matchesInRegime}</div>
                </div>
              </div>
            </>
          ) : (
            <p className={styles.clubSub}>No previous-regime DEMO split is provided for this club.</p>
          )}
        </div>
      </section>

      <div className={styles.tabs}>
        {PROFILE_WINDOWS.map((item) => (
          <button
            type="button"
            key={item.id}
            className={`${styles.tab} ${windowId === item.id ? styles.tabActive : ""}`}
            onClick={() => setWindowId(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className={styles.sectionHead}>
        <div>
          <div className={styles.sectionTitle}>Strongest scenario responses</div>
          <div className={styles.sectionMeta}>Highest SRC in the selected window · click a card for Scenario Lab</div>
        </div>
      </div>
      <div className={styles.cardGrid}>
        {strongest.map((row) => (
          <ScenarioResponseCard
            key={`${row.scenarioId}-${row.metric}-${row.responseWindow}`}
            teamId={dashboard.team.id}
            row={row}
            profileWindow={windowId}
          />
        ))}
      </div>

      <div className={styles.sectionHead}>
        <div>
          <div className={styles.sectionTitle}>Weakest / low-response scenarios</div>
          <div className={styles.sectionMeta}>Lowest SRC — still fixture data, not a trading recommendation</div>
        </div>
      </div>
      <div className={styles.cardGrid}>
        {weakest.map((row) => (
          <ScenarioResponseCard
            key={`${row.scenarioId}-${row.metric}-${row.responseWindow}-weak`}
            teamId={dashboard.team.id}
            row={row}
            profileWindow={windowId}
          />
        ))}
      </div>

      <div className={styles.sectionHead}>
        <div>
          <div className={styles.sectionTitle}>Full profile table</div>
          <div className={styles.sectionMeta}>
            Metric · window · team effect · league benchmark · excess · SRC · N · confidence · quality
          </div>
        </div>
        <span className={`${styles.pill} ${qualityClass(dashboard.team.dataQuality)}`}>
          {qualityLabel(dashboard.team.dataQuality)}
        </span>
      </div>
      <div className={styles.tableWrap}>
        <table>
          <thead>
            <tr>
              <th>Scenario</th>
              <th>Metric</th>
              <th>Window</th>
              <th>Team effect</th>
              <th>League</th>
              <th>Excess</th>
              <th>SRC</th>
              <th>N</th>
              <th>Conf.</th>
              <th>Quality</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const href = scenarioLabHref({
                teamId: dashboard.team.id,
                scenarioId: row.scenarioId,
                metric: row.metric,
                responseWindow: row.responseWindow,
                profileWindow: windowId,
              });
              return (
                <tr className={styles.clickRow} key={`${row.scenarioId}-${row.metric}-${row.responseWindow}-row`}>
                  <td className="row-title">
                    <Link href={href}>{row.scenarioName}</Link>
                  </td>
                  <td>{metricLabel(row.metric)}</td>
                  <td>{row.responseWindow}</td>
                  <td>{formatSigned(row.teamEffect)}</td>
                  <td>{formatSigned(row.leagueEffect)}</td>
                  <td>{formatSigned(row.excessResponse)}</td>
                  <td className={row.src >= 0 ? "edge" : ""}>{formatSigned(row.src)}</td>
                  <td>{row.n}</td>
                  <td>{formatPct(row.confidence)}</td>
                  <td>{qualityLabel(row.dataQuality)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {dashboard.regimeComparison.length > 0 ? (
        <>
          <div className={styles.sectionHead}>
            <div>
              <div className={styles.sectionTitle}>Current regime vs previous regime</div>
              <div className={styles.sectionMeta}>
                Same scenario definitions, split by manager era. DEMO coefficients only.
              </div>
            </div>
          </div>
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>Scenario</th>
                  <th>Metric</th>
                  <th>Window</th>
                  <th>Current SRC</th>
                  <th>Previous SRC</th>
                  <th>Delta</th>
                  <th>Current N</th>
                  <th>Previous N</th>
                </tr>
              </thead>
              <tbody>
                {dashboard.regimeComparison.map((item) => {
                  const delta = Number((item.currentSrc - item.previousSrc).toFixed(2));
                  const href = scenarioLabHref({
                    teamId: dashboard.team.id,
                    scenarioId: item.scenarioId,
                    metric: item.metric,
                    responseWindow: item.responseWindow,
                    profileWindow: "current_manager",
                  });
                  return (
                    <tr className={styles.clickRow} key={item.scenarioId + item.metric}>
                      <td className="row-title">
                        <Link href={href}>{item.scenarioName}</Link>
                      </td>
                      <td>{metricLabel(item.metric)}</td>
                      <td>{item.responseWindow}</td>
                      <td>{formatSigned(item.currentSrc)}</td>
                      <td>{formatSigned(item.previousSrc)}</td>
                      <td className={delta >= 0 ? styles.deltaPos : styles.deltaNeg}>{formatSigned(delta)}</td>
                      <td>{item.currentN}</td>
                      <td>{item.previousN}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      ) : null}

      <section className={styles.notes}>
        {dashboard.notes.map((note) => (
          <div className={styles.note} key={note}>
            {note}
          </div>
        ))}
        <div className={styles.note}>
          Safety: research terminal only. No live betting execution controls are present on Team Explorer.
        </div>
      </section>
    </>
  );
}
