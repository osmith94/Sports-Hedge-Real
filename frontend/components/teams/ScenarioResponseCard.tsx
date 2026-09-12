import Link from "next/link";
import styles from "../../app/teams/teams.module.css";
import {
  formatPct,
  formatSigned,
  metricLabel,
  qualityLabel,
  scenarioLabHref,
  type ProfileWindow,
  type ScenarioResponse,
} from "../../lib/team-fixtures";

function srcClass(src: number): string {
  if (src >= 0.35) return styles.srcPos;
  if (src <= -0.35) return styles.srcNeg;
  return styles.srcNeu;
}

function qualityClass(quality: ScenarioResponse["dataQuality"]): string {
  if (quality === "sufficient") return styles.qualitySufficient;
  if (quality === "limited") return styles.qualityLimited;
  return styles.qualitySparse;
}

export function ScenarioResponseCard({
  teamId,
  row,
  profileWindow,
}: {
  teamId: string;
  row: ScenarioResponse;
  profileWindow: ProfileWindow;
}) {
  const href = scenarioLabHref({
    teamId,
    scenarioId: row.scenarioId,
    metric: row.metric,
    responseWindow: row.responseWindow,
    profileWindow,
  });

  return (
    <Link className={styles.scenarioCard} href={href}>
      <div className={styles.scenarioTop}>
        <div>
          <div className={styles.scenarioName}>{row.scenarioName}</div>
          <div className={styles.scenarioTrigger}>
            {row.trigger.replaceAll("_", " ")} · {metricLabel(row.metric)} · {row.responseWindow} min
          </div>
        </div>
        <div className={`${styles.srcBig} ${srcClass(row.src)}`}>{formatSigned(row.src)}</div>
      </div>
      <div className={styles.gridStats}>
        <div className={styles.stat}>
          <div className={styles.statLabel}>Team effect</div>
          <div className={styles.statValue}>{formatSigned(row.teamEffect)}</div>
        </div>
        <div className={styles.stat}>
          <div className={styles.statLabel}>League benchmark</div>
          <div className={styles.statValue}>{formatSigned(row.leagueEffect)}</div>
        </div>
        <div className={styles.stat}>
          <div className={styles.statLabel}>Excess</div>
          <div className={styles.statValue}>{formatSigned(row.excessResponse)}</div>
        </div>
        <div className={styles.stat}>
          <div className={styles.statLabel}>N / conf.</div>
          <div className={styles.statValue}>
            {row.n} · {formatPct(row.confidence)}
          </div>
        </div>
      </div>
      <div className={styles.fixtureLine}>
        <span className={`${styles.pill} ${qualityClass(row.dataQuality)}`}>{qualityLabel(row.dataQuality)}</span>
        <span className={styles.labLink}>Open in Scenario Lab →</span>
      </div>
    </Link>
  );
}
