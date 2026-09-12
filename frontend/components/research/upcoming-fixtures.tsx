import Link from "next/link";
import styles from "../../app/research/research.module.css";
import {
  bestAvailableQuote,
  evPerPound,
  formatEv,
  formatPercent,
  researchFixtures,
  strongestSignalForFixture,
  teamHref,
  type ResearchFixture,
} from "../../lib/research-fixtures";

function FixtureCard({ fixture }: { fixture: ResearchFixture }) {
  const signal = strongestSignalForFixture(fixture);
  const best = signal ? bestAvailableQuote(signal) : undefined;
  const ev = signal && best?.netDecimal != null ? evPerPound(signal.modelProbability, best.netDecimal) : null;

  return (
    <article className={`${styles.fixtureCard} ${fixture.featured ? styles.fixtureCardFeatured : ""}`}>
      <div className={styles.fixtureTop}>
        <div>
          <div className={styles.comp}>
            {fixture.competition}
            {fixture.featured ? " · featured 12:30" : ""}
          </div>
          <div className={styles.teams}>
            <Link className={styles.rowLink} href={teamHref(fixture.home.teamId)}>
              {fixture.home.name}
            </Link>
            <span className={styles.vs}>v</span>
            <Link className={styles.rowLink} href={teamHref(fixture.away.teamId)}>
              {fixture.away.name}
            </Link>
          </div>
        </div>
        <div className={styles.kickoff}>{fixture.kickoffLocal}</div>
      </div>

      <div className={styles.metaLine}>{fixture.favouriteRoleNote}</div>
      <div className={styles.metaLine}>
        Implied (demo): {fixture.home.shortName} {formatPercent(fixture.homeImpliedProbability, 0)} ·{" "}
        {fixture.away.shortName} {formatPercent(fixture.awayImpliedProbability, 0)}
      </div>
      <div className={styles.metaLine}>
        {fixture.homeManager} ({fixture.homeManagerPhase.toLowerCase()}) · {fixture.awayManager} (
        {fixture.awayManagerPhase.toLowerCase()})
      </div>
      <div className={styles.muted}>{fixture.regimeStatus}</div>

      <div className={styles.statRow}>
        <div className={styles.stat}>
          <div className={styles.statLabel}>Relevant scenarios</div>
          <div className={styles.statValue}>{fixture.relevantScenarioCount}</div>
        </div>
        <div className={styles.stat}>
          <div className={styles.statLabel}>Strongest odds-weighted signal</div>
          <div className={styles.statValue}>{signal ? signal.scenarioTitle : "—"}</div>
        </div>
        <div className={styles.stat}>
          <div className={styles.statLabel}>Est. EV / £1 (demo)</div>
          <div className={`${styles.statValue} ${ev !== null && ev > 0 ? styles.positive : ""}`}>
            {ev === null ? "—" : formatEv(ev)}
          </div>
        </div>
      </div>

      <div className={styles.rowActions}>
        <Link className={styles.ghostLink} href={fixture.matchdayHref}>
          Open Matchday
        </Link>
        <Link className={styles.ghostLink} href={teamHref(fixture.home.teamId)}>
          {fixture.home.shortName} profile
        </Link>
        <Link className={styles.ghostLink} href={teamHref(fixture.away.teamId)}>
          {fixture.away.shortName} profile
        </Link>
        <span className={`${styles.chip} ${styles.chipWarn}`}>DEMO / FIXTURE DATA</span>
      </div>
    </article>
  );
}

export function UpcomingFixtures() {
  return (
    <section className={styles.panel}>
      <div className={styles.panelHeader}>
        <div>
          <div className={styles.panelTitle}>Upcoming fixtures</div>
          <div className={styles.panelMeta}>
            Premier League, Championship, La Liga and Champions League-ready cards. Kickoff, regime and the strongest
            odds-weighted demo signal — not an SRC-only ranking.
          </div>
        </div>
      </div>
      <div className={styles.fixtureGrid}>
        {researchFixtures.map((fixture) => (
          <FixtureCard key={fixture.fixtureId} fixture={fixture} />
        ))}
      </div>
    </section>
  );
}
