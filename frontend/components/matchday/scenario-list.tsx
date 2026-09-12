import Link from "next/link";
import styles from "../../app/matchday/matchday.module.css";
import {
  scenarioLabHref,
  srcLabel,
  type ScenarioProfile,
} from "../../lib/matchday-fixtures";

function qualityClass(quality: ScenarioProfile["dataQuality"]): string {
  if (quality === "sufficient") return styles.qualitySufficient;
  if (quality === "thin") return styles.qualityThin;
  return styles.qualityIllustrative;
}

function srcClass(callout: ScenarioProfile["callout"]): string {
  if (callout === "high") return styles.srcHigh;
  if (callout === "low") return styles.srcLow;
  return "";
}

export function ScenarioList({ scenarios }: { scenarios: ScenarioProfile[] }) {
  const high = scenarios.find((item) => item.callout === "high");
  const low = scenarios.find((item) => item.callout === "low");

  return (
    <div className={styles.section}>
      <div className={styles.sectionTitle}>Scenarios to inspect</div>
      <div className={styles.callouts}>
        {high ? (
          <div className={`${styles.callout} ${styles.calloutHigh}`}>
            <div className={styles.calloutKicker}>High SRC · demo</div>
            <div className={styles.calloutTitle}>{high.title}</div>
            <div className={styles.calloutMeta}>
              {high.metric} · {high.window} · SRC {srcLabel(high.src)} · N={high.sampleSize} · {high.confidence} confidence
            </div>
          </div>
        ) : (
          <div className={styles.callout}>
            <div className={styles.calloutKicker}>High SRC</div>
            <div className={styles.calloutTitle}>No high-SRC demo profile on this card</div>
          </div>
        )}
        {low ? (
          <div className={`${styles.callout} ${styles.calloutLow}`}>
            <div className={styles.calloutKicker}>Low SRC · demo</div>
            <div className={styles.calloutTitle}>{low.title}</div>
            <div className={styles.calloutMeta}>
              {low.metric} · {low.window} · SRC {srcLabel(low.src)} · N={low.sampleSize} · {low.dataQuality} sample
            </div>
          </div>
        ) : (
          <div className={styles.callout}>
            <div className={styles.calloutKicker}>Low SRC</div>
            <div className={styles.calloutTitle}>No low-SRC demo profile on this card</div>
          </div>
        )}
      </div>

      <div className={styles.scenarioList}>
        {scenarios.map((scenario) => (
          <div className={styles.scenarioRow} key={`${scenario.teamId}-${scenario.scenarioId}-${scenario.metric}`}>
            <div>
              <div className={styles.scenarioTitle}>{scenario.title}</div>
              <div className={styles.scenarioDetail}>
                {scenario.teamId} · {scenario.trigger} · {scenario.metric} · window {scenario.window}
              </div>
            </div>
            <div>
              <div className={styles.statLabel}>SRC</div>
              <div className={`${styles.statValue} ${srcClass(scenario.callout)}`}>{srcLabel(scenario.src)}</div>
            </div>
            <div>
              <div className={styles.statLabel}>N / confidence</div>
              <div className={styles.statValue}>
                {scenario.sampleSize} · {scenario.confidence}
              </div>
            </div>
            <div>
              <div className={styles.statLabel}>Data quality</div>
              <div className={`${styles.statValue} ${qualityClass(scenario.dataQuality)}`}>{scenario.dataQuality}</div>
            </div>
            <div>
              <div className={styles.statLabel}>Excess vs league</div>
              <div className={styles.statValue}>{scenario.teamExcessResponse}</div>
            </div>
            <Link className={styles.labLink} href={scenarioLabHref(scenario)}>
              Open in Scenario Lab
            </Link>
          </div>
        ))}
      </div>
    </div>
  );
}
