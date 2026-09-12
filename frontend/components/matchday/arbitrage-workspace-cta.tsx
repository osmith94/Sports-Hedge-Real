import Link from "next/link";
import styles from "../../app/matchday/matchday.module.css";

export function ArbitrageWorkspaceCta() {
  return (
    <section className={styles.arb}>
      <div>
        <div className={styles.arbTitle}>Arbitrage workspace — separate module</div>
        <p className={styles.arbCopy}>
          Matchday research ranks fixtures, regime context and Scenario Response Profiles. It does not mix those
          coefficients into an arbitrage model, and it does not claim a current executable opportunity. Paper scans
          and cross-venue matching live in the arbitrage workspace.
        </p>
      </div>
      <Link className={styles.arbLink} href="/">
        Open arbitrage workspace
      </Link>
    </section>
  );
}
