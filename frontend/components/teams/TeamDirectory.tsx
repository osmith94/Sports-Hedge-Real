"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import styles from "../../app/teams/teams.module.css";
import {
  COMPETITIONS,
  getCompetition,
  listTeams,
  qualityLabel,
  type CompetitionId,
} from "../../lib/team-fixtures";
import { DemoDataBanner } from "./DemoDataBanner";

export function TeamDirectory() {
  const teams = listTeams();
  const [query, setQuery] = useState("");
  const [league, setLeague] = useState<CompetitionId | "all">("all");

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return teams.filter((team) => {
      const competition = getCompetition(team.competitionId);
      const leagueOk = league === "all" || team.competitionId === league;
      const text = `${team.name} ${team.shortName} ${team.city} ${competition.name}`.toLowerCase();
      return leagueOk && (needle.length === 0 || text.includes(needle));
    });
  }, [league, query, teams]);

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Team Explorer</div>
          <h1>Club directory</h1>
          <p className="page-subtitle">
            Browse clubs like a Football Manager squad screen, then open a research dashboard for Scenario Response
            Profiles, manager-era context and next-kickoff setup. Analysis only — no live betting controls.
          </p>
        </div>
        <div className="demo-label">DEMO / FIXTURE DATA</div>
      </div>

      <DemoDataBanner />

      <div className={styles.searchRow}>
        <input
          className={styles.searchInput}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search club, city or short code — try Arsenal"
          aria-label="Search teams"
        />
        <div className={styles.leagueChips}>
          <button
            type="button"
            className={`${styles.chip} ${league === "all" ? styles.chipActive : ""}`}
            onClick={() => setLeague("all")}
          >
            All competitions
          </button>
          {COMPETITIONS.map((competition) => (
            <button
              type="button"
              key={competition.id}
              className={`${styles.chip} ${league === competition.id ? styles.chipActive : ""}`}
              onClick={() => setLeague(competition.id)}
            >
              {competition.name}
            </button>
          ))}
        </div>
      </div>

      <section className={styles.directory}>
        {filtered.length === 0 ? (
          <div className={styles.empty}>No clubs match that filter in the DEMO universe.</div>
        ) : (
          filtered.map((team) => {
            const competition = getCompetition(team.competitionId);
            return (
              <Link className={styles.teamCard} href={`/teams/${team.id}`} key={team.id}>
                <div className={styles.teamCardTop}>
                  <div style={{ display: "flex", gap: 11, alignItems: "center" }}>
                    <div className={styles.crest}>{team.shortName}</div>
                    <div>
                      <div className={styles.teamName}>{team.name}</div>
                      <div className={styles.teamMeta}>
                        {competition.name} · {team.city}
                      </div>
                    </div>
                  </div>
                  <div className={styles.srcIndex}>
                    <div className={styles.srcValue}>{team.srcIndex}</div>
                    <div className={styles.srcLabel}>SRC index</div>
                  </div>
                </div>
                <div className={styles.teamStats}>
                  <div className={styles.stat}>
                    <div className={styles.statLabel}>Primary metric</div>
                    <div className={styles.statValue}>{team.primaryMetric.replace("_", " ")}</div>
                  </div>
                  <div className={styles.stat}>
                    <div className={styles.statLabel}>Sample</div>
                    <div className={styles.statValue}>N={team.sampleSize}</div>
                  </div>
                  <div className={styles.stat}>
                    <div className={styles.statLabel}>Data quality</div>
                    <div className={styles.statValue}>{qualityLabel(team.dataQuality)}</div>
                  </div>
                </div>
              </Link>
            );
          })
        )}
      </section>
    </>
  );
}
