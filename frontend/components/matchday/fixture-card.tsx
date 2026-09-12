import Link from "next/link";
import styles from "../../app/matchday/matchday.module.css";
import { MATCHDAY_HUB, teamHref, type MatchdayFixture } from "../../lib/matchday-fixtures";
import { RegimeContext } from "./regime-context";
import { ScenarioList } from "./scenario-list";
import { valueForScenario } from "../../lib/demo/value-signals";
import { ValueOverlay } from "../research/value-overlay";

export function FixtureCard({ fixture }: { fixture: MatchdayFixture }) {
  return (
    <article className={`${styles.card} ${fixture.featured ? styles.cardFeatured : ""}`}>
      <header className={styles.cardHeader}>
        <div>
          <div className={styles.compRow}>
            <span>{fixture.competition}</span>
            <span className={styles.kickoff}>{fixture.kickoffLabel}</span>
            <span>{fixture.venue}</span>
            {fixture.featured ? <span>Featured research card</span> : null}
          </div>
          <div className={styles.teams}>
            <Link className={styles.teamLink} href={teamHref(fixture.home.teamId)}>
              {fixture.home.name}
            </Link>
            <span className={styles.versus}>v</span>
            <Link className={styles.teamLink} href={teamHref(fixture.away.teamId)}>
              {fixture.away.name}
            </Link>
          </div>
          <p className={styles.contextNote}>{fixture.preMatchNote}</p>
        </div>
        <div className={styles.headerMeta}>
          <span className={styles.demoBadge}>{MATCHDAY_HUB.dataLabel}</span>
          <span className={styles.paperBadge}>{MATCHDAY_HUB.paperMode}</span>
        </div>
      </header>

      <RegimeContext home={fixture.home} away={fixture.away} />
      {fixture.featured
        ? fixture.scenarios
            .map((scenario) => valueForScenario({ teamId: scenario.teamId, scenarioId: scenario.scenarioId }))
            .filter((signal): signal is NonNullable<typeof signal> => Boolean(signal))
            .slice(0, 2)
            .map((signal) => <ValueOverlay key={signal.signalId} signal={signal} />)
        : null}
      <ScenarioList scenarios={fixture.scenarios} />
    </article>
  );
}
