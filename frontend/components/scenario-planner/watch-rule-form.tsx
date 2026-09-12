"use client";

import { canonicalResponseWindow } from "../../lib/demo/ids";
import {
  COMPETITIONS,
  DATA_QUALITIES,
  MARKET_FAMILIES,
  METRICS,
  REGIME_REQUIREMENTS,
  RESPONSE_WINDOWS,
  SCENARIOS,
  TEAMS,
} from "../../lib/scenario-planner-fixtures";
import { EMPTY_DRAFT, WatchRuleDraft } from "./use-paper-watch-rules";
import styles from "./scenario-planner.module.css";

type Props = {
  draft: WatchRuleDraft;
  editingId: string | null;
  onChange: (draft: WatchRuleDraft) => void;
  onSubmit: () => void;
  onCancel: () => void;
};

function numberOrNull(value: string): number | null {
  if (value.trim() === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function WatchRuleForm({ draft, editingId, onChange, onSubmit, onCancel }: Props) {
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <div className={styles.formGrid}>
        <label className={styles.field}>
          <span>Team</span>
          <select
            value={draft.teamId}
            onChange={(event) => onChange({ ...draft, teamId: event.target.value })}
          >
            {TEAMS.map((team) => (
              <option key={team.id} value={team.id}>
                {team.name}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span>Scenario</span>
          <select
            value={draft.scenarioId}
            onChange={(event) => onChange({ ...draft, scenarioId: event.target.value })}
          >
            {SCENARIOS.map((scenario) => (
              <option key={scenario.id} value={scenario.id}>
                {scenario.label}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span>Metric</span>
          <select
            value={draft.metric}
            onChange={(event) => onChange({ ...draft, metric: event.target.value as WatchRuleDraft["metric"] })}
          >
            {METRICS.map((metric) => (
              <option key={metric.id} value={metric.id}>
                {metric.label}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span>Response window</span>
          <select
            value={draft.responseWindow}
            onChange={(event) => {
              const window = canonicalResponseWindow(event.target.value);
              if (!window) return;
              onChange({ ...draft, responseWindow: window });
            }}
          >
            {RESPONSE_WINDOWS.map((window) => (
              <option key={window.id} value={window.id}>
                {window.label}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span>Competition</span>
          <select
            value={draft.competition}
            onChange={(event) => onChange({ ...draft, competition: event.target.value })}
          >
            {COMPETITIONS.map((competition) => (
              <option key={competition.id} value={competition.id}>
                {competition.label}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span>Manager / regime</span>
          <select
            value={draft.regimeRequirement}
            onChange={(event) =>
              onChange({
                ...draft,
                regimeRequirement: event.target.value as WatchRuleDraft["regimeRequirement"],
              })
            }
          >
            {REGIME_REQUIREMENTS.map((regime) => (
              <option key={regime.id} value={regime.id}>
                {regime.label}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span>Minimum SRC</span>
          <input
            type="number"
            min={0}
            max={1}
            step="0.01"
            value={draft.minSrc}
            onChange={(event) => onChange({ ...draft, minSrc: Number(event.target.value) })}
          />
        </label>
        <label className={styles.field}>
          <span>Minimum sample size</span>
          <input
            type="number"
            min={0}
            step="1"
            value={draft.minSampleSize}
            onChange={(event) => onChange({ ...draft, minSampleSize: Number(event.target.value) })}
          />
        </label>
        <label className={styles.field}>
          <span>Minimum confidence</span>
          <input
            type="number"
            min={0}
            max={1}
            step="0.01"
            value={draft.minConfidence}
            onChange={(event) => onChange({ ...draft, minConfidence: Number(event.target.value) })}
          />
        </label>
        <label className={styles.field}>
          <span>Minimum data quality</span>
          <select
            value={draft.minDataQuality}
            onChange={(event) =>
              onChange({ ...draft, minDataQuality: event.target.value as WatchRuleDraft["minDataQuality"] })
            }
          >
            {DATA_QUALITIES.map((quality) => (
              <option key={quality.id} value={quality.id}>
                {quality.label}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span>Market family of interest</span>
          <select
            value={draft.marketFamily}
            onChange={(event) =>
              onChange({ ...draft, marketFamily: event.target.value as WatchRuleDraft["marketFamily"] })
            }
          >
            {MARKET_FAMILIES.map((family) => (
              <option key={family.id} value={family.id}>
                {family.label}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span>Min probability edge (pp)</span>
          <input
            type="number"
            min={0}
            step="0.1"
            value={draft.minProbabilityEdgePp}
            onChange={(event) => onChange({ ...draft, minProbabilityEdgePp: Number(event.target.value) })}
          />
        </label>
        <label className={styles.field}>
          <span>Min EV / £1 (net odds)</span>
          <input
            type="number"
            min={0}
            step="0.001"
            value={draft.minEvPerPound}
            onChange={(event) => onChange({ ...draft, minEvPerPound: Number(event.target.value) })}
          />
        </label>
        <label className={styles.field}>
          <span>Max quote age (minutes)</span>
          <input
            type="number"
            min={1}
            step="1"
            value={draft.maxQuoteAgeMinutes}
            onChange={(event) => onChange({ ...draft, maxQuoteAgeMinutes: Number(event.target.value) })}
          />
        </label>
        <label className={styles.field}>
          <span>Known fees required</span>
          <select
            value={draft.requireKnownFees ? "yes" : "no"}
            onChange={(event) => onChange({ ...draft, requireKnownFees: event.target.value === "yes" })}
          >
            <option value="yes">Yes · MISSING_COSTS fails closed</option>
            <option value="no">No (not recommended)</option>
          </select>
        </label>
        <label className={styles.field}>
          <span>Paper stake cap (£)</span>
          <input
            type="number"
            min={0}
            step="1"
            value={draft.paperStakeCapGbp ?? ""}
            onChange={(event) => onChange({ ...draft, paperStakeCapGbp: numberOrNull(event.target.value) })}
            placeholder="Optional"
          />
        </label>
        <label className={styles.field}>
          <span>Bankroll %</span>
          <input
            type="number"
            min={0}
            max={100}
            step="0.1"
            value={draft.bankrollPercent ?? ""}
            onChange={(event) => onChange({ ...draft, bankrollPercent: numberOrNull(event.target.value) })}
            placeholder="Optional"
          />
        </label>
        <label className={`${styles.field} ${styles.fieldFull}`}>
          <span>Notes</span>
          <textarea
            value={draft.notes}
            onChange={(event) => onChange({ ...draft, notes: event.target.value })}
            placeholder="Planning notes only. This does not send an order."
          />
        </label>
      </div>
      <div className={styles.formActions}>
        <button className={styles.button} type="submit">
          {editingId ? "Save paper rule" : "Add paper rule"}
        </button>
        <button
          className={styles.buttonGhost}
          type="button"
          onClick={() => {
            onChange({ ...EMPTY_DRAFT });
            onCancel();
          }}
        >
          Cancel
        </button>
      </div>
      <p className={styles.note}>
        Rules stay in this browser. They never place bets, request venue credentials, or mark a DEMO snapshot as a live fill.
      </p>
    </form>
  );
}
