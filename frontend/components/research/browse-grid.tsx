import Link from "next/link";
import styles from "../../app/research/research.module.css";
import { researchBrowse } from "../../lib/research-fixtures";

export function BrowseGrid() {
  return (
    <section className={styles.panel}>
      <div className={styles.panelHeader}>
        <div>
          <div className={styles.panelTitle}>Browse research</div>
          <div className={styles.panelMeta}>Drill into team, matchday, Scenario Lab and market-history surfaces.</div>
        </div>
      </div>
      <div className={styles.browseGrid}>
        {researchBrowse.map((item) => (
          <Link className={styles.browseCard} href={item.href} key={item.href}>
            <span className={styles.browseIcon}>{item.icon}</span>
            <div className={styles.browseLabel}>{item.label}</div>
            <div className={styles.browseCopy}>{item.description}</div>
          </Link>
        ))}
      </div>
    </section>
  );
}
