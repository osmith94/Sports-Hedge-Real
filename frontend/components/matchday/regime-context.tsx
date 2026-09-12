import Link from "next/link";
import styles from "../../app/matchday/matchday.module.css";
import {
  MATCHDAY_HUB,
  formatImpliedProbability,
  teamHref,
  type MatchdayTeamContext,
} from "../../lib/matchday-fixtures";

function roleLabel(role: MatchdayTeamContext["role"]): string {
  if (role === "favourite") return "Pre-match favourite · demo";
  if (role === "underdog") return "Pre-match underdog · demo";
  return "Even market · demo";
}

export function RegimeContext({ home, away }: { home: MatchdayTeamContext; away: MatchdayTeamContext }) {
  return (
    <div className={styles.section}>
      <div className={styles.sectionTitle}>Team / regime context</div>
      <div className={styles.regimeGrid}>
        {[home, away].map((team) => (
          <article className={styles.regimeCard} key={team.teamId}>
            <div className={styles.regimeTop}>
              <div>
                <div className={styles.role}>{roleLabel(team.role)}</div>
                <Link className={`${styles.teamLink} ${styles.regimeTeam}`} href={teamHref(team.teamId)}>
                  {team.name}
                </Link>
              </div>
              <div>
                <div className={styles.prob}>{formatImpliedProbability(team.demoImpliedProbability)}</div>
                <div className={styles.probFoot}>demo implied win</div>
              </div>
            </div>
            <div className={styles.manager}>
              {team.managerName} · {team.appointmentType}
            </div>
            <div className={styles.regimeMeta}>
              <span>
                Manager phase {team.managerPhase} · match {team.managerMatchNumber} in tenure
              </span>
              <span>
                Season phase {team.seasonPhase} · season match {team.seasonMatchNumber}
              </span>
              <span>{team.regimeNote}</span>
            </div>
            {team.regimeChangeRecent ? (
              <div className={styles.flag}>Recent regime marker — do not blend prior-manager history.</div>
            ) : null}
          </article>
        ))}
      </div>
      <div className={styles.contextNote}>
        {MATCHDAY_HUB.dataLabel}: implied probabilities and tenure counts are fixture placeholders for browsing, not a live market snapshot.
      </div>
    </div>
  );
}
