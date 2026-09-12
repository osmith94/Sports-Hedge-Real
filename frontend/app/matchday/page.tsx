import { ArbitrageWorkspaceCta } from "../../components/matchday/arbitrage-workspace-cta";
import { FixtureCard } from "../../components/matchday/fixture-card";
import {
  MATCHDAY_DATA_DISCLAIMER,
  MATCHDAY_HUB,
  matchdayFixtures,
} from "../../lib/matchday-fixtures";
import styles from "./matchday.module.css";

export default function MatchdayPage() {
  return (
    <div className={styles.page}>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Matchday research hub</div>
          <h1>
            {MATCHDAY_HUB.dateLabel} · {MATCHDAY_HUB.dayLabel}
          </h1>
          <p className="page-subtitle">
            Upcoming fixtures for pre-match browsing: team drill-down, manager-era context, and Scenario Response
            Profiles to inspect in Scenario Lab. Analysis only — not a pricing or execution screen.
          </p>
        </div>
        <div className="demo-label">{MATCHDAY_HUB.dataLabel}</div>
      </div>

      <div className={styles.metaRow}>
        <span className={`${styles.chip} ${styles.chipAccent}`}>{MATCHDAY_HUB.paperMode}</span>
        <span className={`${styles.chip} ${styles.chipWarn}`}>{MATCHDAY_HUB.dataLabel}</span>
        <span className={styles.chip}>{MATCHDAY_HUB.localTimeContext}</span>
        <span className={styles.chip}>{matchdayFixtures.length} demo fixtures</span>
      </div>

      <div className={styles.honesty}>{MATCHDAY_DATA_DISCLAIMER}</div>

      <section className={styles.list} aria-label="Upcoming fixtures">
        {matchdayFixtures.map((fixture) => (
          <FixtureCard fixture={fixture} key={fixture.fixtureId} />
        ))}
      </section>

      <ArbitrageWorkspaceCta />
    </div>
  );
}
