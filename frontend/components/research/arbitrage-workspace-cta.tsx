import Link from "next/link";
import styles from "../../app/research/research.module.css";
import { arbitrageWorkspace } from "../../lib/research-fixtures";

export function ArbitrageWorkspaceCta() {
  return (
    <section className={styles.panel}>
      <div className={styles.arb}>
        <div className={styles.arbCopy}>
          <div className={styles.arbTitle}>{arbitrageWorkspace.title}</div>
          <div className={styles.arbStatus}>{arbitrageWorkspace.status}</div>
          <div className={styles.arbNote}>{arbitrageWorkspace.note}</div>
        </div>
        <Link className={styles.arbCta} href={arbitrageWorkspace.href}>
          Open Arbitrage Workspace
        </Link>
      </div>
    </section>
  );
}
