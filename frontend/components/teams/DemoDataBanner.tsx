import styles from "../../app/teams/teams.module.css";
import { DEMO_FIXTURE_BANNER, DEMO_FIXTURE_DISCLAIMER } from "../../lib/team-fixtures";

export function DemoDataBanner() {
  return (
    <aside className={styles.banner} role="status">
      <span className={styles.bannerMark}>{DEMO_FIXTURE_BANNER}</span>
      <p className={styles.bannerCopy}>{DEMO_FIXTURE_DISCLAIMER}</p>
    </aside>
  );
}
