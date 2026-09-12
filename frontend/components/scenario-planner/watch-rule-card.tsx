"use client";

import Link from "next/link";
import {
  PaperWatchRule,
  competitionLabel,
  marketFamilyLabel,
  summarizeRule,
} from "../../lib/scenario-planner-fixtures";
import styles from "./scenario-planner.module.css";

type Props = {
  rule: PaperWatchRule;
  onEdit: (rule: PaperWatchRule) => void;
  onToggle: (id: string) => void;
  onDelete: (id: string) => void;
};

export function WatchRuleCard({ rule, onEdit, onToggle, onDelete }: Props) {
  const cap =
    rule.paperStakeCapGbp === null
      ? "No paper stake cap"
      : `Paper stake cap £${rule.paperStakeCapGbp}`;
  const bankroll =
    rule.bankrollPercent === null ? "No bankroll %" : `${rule.bankrollPercent}% bankroll`;

  return (
    <article className={`${styles.card} ${rule.active ? "" : styles.cardInactive}`}>
      <div className={styles.cardTop}>
        <div>
          <div className={styles.cardTitle}>{rule.teamName}</div>
          <p className={styles.cardSummary}>{summarizeRule(rule)}</p>
        </div>
        <span className={rule.active ? styles.pillActive : styles.pillInactive}>
          {rule.active ? "ACTIVE" : "INACTIVE"}
        </span>
      </div>
      <div className={styles.metaRow}>
        <span className={styles.pill}>{competitionLabel(rule.competition)}</span>
        <span className={styles.pill}>{marketFamilyLabel(rule.marketFamily)}</span>
        <span className={styles.pill}>min confidence {rule.minConfidence.toFixed(2)}</span>
        <span className={styles.pill}>min quality {rule.minDataQuality}</span>
        <span className={styles.pill}>{cap}</span>
        <span className={styles.pill}>{bankroll}</span>
      </div>
      {rule.notes ? <p className={styles.note}>{rule.notes}</p> : null}
      <div className={styles.actions}>
        <button className={styles.buttonGhost} type="button" onClick={() => onToggle(rule.id)}>
          {rule.active ? "Set inactive" : "Set active"}
        </button>
        <button className={styles.buttonGhost} type="button" onClick={() => onEdit(rule)}>
          Edit
        </button>
        <button className={styles.buttonDanger} type="button" onClick={() => onDelete(rule.id)}>
          Delete
        </button>
        <Link className={styles.linkChip} href={`/teams/${rule.teamId}`}>
          Team profile
        </Link>
        <Link className={styles.linkChip} href="/scenario-lab">
          Scenario Lab
        </Link>
      </div>
    </article>
  );
}
