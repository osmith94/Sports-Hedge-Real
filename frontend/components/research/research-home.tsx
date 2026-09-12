import styles from "../../app/research/research.module.css";
import {
  RESEARCH_DEMO_BANNER,
  RESEARCH_HUB,
  bestAvailableQuote,
  evPerPound,
  formatEv,
  rankedResearchSignals,
} from "../../lib/research-fixtures";
import { ArbitrageWorkspaceCta } from "./arbitrage-workspace-cta";
import { BrowseGrid } from "./browse-grid";
import { DemoBanner } from "./demo-banner";
import { UpcomingFixtures } from "./upcoming-fixtures";
import { ValueSignals } from "./value-signals";
import { HistoricalCoverageSeam } from "./historical-coverage";

export function ResearchHome() {
  const top = rankedResearchSignals[0];
  const topQuote = top ? bestAvailableQuote(top) : undefined;
  const topEv = top && topQuote?.netDecimal != null ? evPerPound(top.modelProbability, topQuote.netDecimal) : null;

  return (
    <div className={styles.page}>
      <div className={styles.heading}>
        <div>
          <div className={styles.eyebrow}>Research</div>
          <h1 className={styles.title}>Research Home</h1>
          <p className={styles.subtitle}>
            Snapshot before kickoff: odds-weighted scenario value, upcoming fixtures, team/regime context, then drill-down.
            Separate from the Arbitrage workspace.
          </p>
        </div>
        <div className={styles.headingBadges}>
          <span className={`${styles.badge} ${styles.badgePaper}`}>{RESEARCH_HUB.paperMode}</span>
          <span className={`${styles.badge} ${styles.badgeDemo}`}>{RESEARCH_DEMO_BANNER}</span>
        </div>
      </div>

      <DemoBanner />

      <section className={styles.metricGrid}>
        <div className={styles.metricCard}>
          <div className={styles.metricLabel}>Snapshot</div>
          <div className={styles.metricValue}>11:30</div>
          <div className={styles.metricFoot}>{RESEARCH_HUB.snapshotLabel}</div>
        </div>
        <div className={styles.metricCard}>
          <div className={styles.metricLabel}>Next kickoff</div>
          <div className={styles.metricValue}>12:30</div>
          <div className={styles.metricFoot}>{RESEARCH_HUB.kickoffContext}</div>
        </div>
        <div className={styles.metricCard}>
          <div className={styles.metricLabel}>Top demo EV / £1</div>
          <div className={`${styles.metricValue} ${styles.positive}`}>{topEv === null ? "—" : formatEv(topEv)}</div>
          <div className={styles.metricFoot}>{top ? `${top.teamName} · ${top.marketFamily}` : "No ranked signal"}</div>
        </div>
        <div className={styles.metricCard}>
          <div className={styles.metricLabel}>Ranking rule</div>
          <div className={styles.metricValue}>Odds</div>
          <div className={styles.metricFoot}>{RESEARCH_HUB.rankingRule} High SRC can still be NO_VALUE.</div>
        </div>
      </section>

      <ValueSignals />
      <UpcomingFixtures />
      <BrowseGrid />
      <HistoricalCoverageSeam />
      <ArbitrageWorkspaceCta />
    </div>
  );
}
