"use client";

import {
  DemoScenarioSnapshot,
  PaperWatchRule,
  competitionLabel,
  metricLabel,
  ruleWouldMatch,
  scenarioLabel,
  summarizeRule,
  windowLabel,
} from "../../lib/scenario-planner-fixtures";
import styles from "./scenario-planner.module.css";

type Props = {
  snapshots: DemoScenarioSnapshot[];
  rules: PaperWatchRule[];
};

export function DemoMatchPreview({ snapshots, rules }: Props) {
  return (
    <div className={styles.demoList}>
      {snapshots.map((snapshot) => {
        const matches = rules.filter((rule) => ruleWouldMatch(rule, snapshot));
        return (
          <article className={styles.demoRow} key={snapshot.id}>
            <div className={styles.demoTop}>
              <div>
                <div className={styles.demoTitle}>
                  {snapshot.teamName} vs {snapshot.opponent} · {snapshot.scoreline} · {snapshot.matchMinute}&apos;
                </div>
                <div className={styles.demoMeta}>
                  {scenarioLabel(snapshot.scenarioId)} · {metricLabel(snapshot.metric)} ·{" "}
                  {windowLabel(snapshot.responseWindow)} · {competitionLabel(snapshot.competition)} · SRC{" "}
                  {snapshot.src.toFixed(2)} · N={snapshot.sampleSize} · {snapshot.valueState.replaceAll("_", " ")} · EV/£1{" "}
                  {snapshot.evPerPound === null ? "n/a" : snapshot.evPerPound.toFixed(3)}
                </div>
              </div>
              <span className={styles.pillDemo}>{snapshot.demoLabel}</span>
            </div>
            <div className={`${styles.matchResult} ${matches.length ? styles.matchYes : styles.matchNo}`}>
              {matches.length
                ? `Would match ${matches.length} active paper rule${matches.length === 1 ? "" : "s"} (fixture preview only): ${matches.map(summarizeRule).join(" ")}`
                : "Would not match any active paper rule on this fixture snapshot."}
            </div>
            <p className={styles.note}>
              Fixture preview only. This is not a live market trigger, not a paper fill, and not an order.
            </p>
          </article>
        );
      })}
    </div>
  );
}
