import styles from "../../app/research/research.module.css";
import { RESEARCH_DEMO_DISCLAIMER } from "../../lib/research-fixtures";

export function DemoBanner() {
  return <div className={styles.banner}>{RESEARCH_DEMO_DISCLAIMER}</div>;
}
