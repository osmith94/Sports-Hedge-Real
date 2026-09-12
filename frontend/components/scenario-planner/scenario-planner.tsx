"use client";

import { useState } from "react";
import Link from "next/link";
import {
  DEMO_SCENARIO_SNAPSHOTS,
  PLANNER_PERSISTENCE_SEAM,
  PaperWatchRule,
} from "../../lib/scenario-planner-fixtures";
import { DemoMatchPreview } from "./demo-match-preview";
import { EMPTY_DRAFT, WatchRuleDraft, usePaperWatchRules } from "./use-paper-watch-rules";
import { WatchRuleCard } from "./watch-rule-card";
import { WatchRuleForm } from "./watch-rule-form";
import styles from "./scenario-planner.module.css";

function draftFromRule(rule: PaperWatchRule): WatchRuleDraft {
  const { id, createdAt, updatedAt, teamName, ...rest } = rule;
  void createdAt;
  void updatedAt;
  void teamName;
  return { id, ...rest };
}

export function ScenarioPlanner() {
  const { rules, hydrated, upsert, toggle, remove, resetToSeed, activeCount } = usePaperWatchRules();
  const [draft, setDraft] = useState<WatchRuleDraft>({ ...EMPTY_DRAFT });
  const [editingId, setEditingId] = useState<string | null>(null);

  return (
    <div className={styles.page}>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Scenario Planner</div>
          <h1>Scenario watchlist and paper strategy</h1>
          <p className="page-subtitle">
            Define how you want to monitor Scenario Response Profiles and paper-manage responses. This slice is
            local, read-only against live venues, and never places a bet.
          </p>
          <div className={styles.links}>
            <Link className={styles.linkChip} href="/scenario-lab">
              Open Scenario Lab
            </Link>
            <Link className={styles.linkChip} href="/teams/arsenal">
              Arsenal team profile
            </Link>
          </div>
        </div>
        <div className={styles.badges}>
          <span className="demo-label">PAPER MODE</span>
          <span className="demo-label">DEMO / FIXTURE DATA</span>
        </div>
      </div>

      <section className="metric-grid">
        <div className="metric-card">
          <div className="metric-label">Paper rules</div>
          <div className="metric-value">{hydrated ? rules.length : "—"}</div>
          <div className="metric-foot">Stored in this browser only</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Active watches</div>
          <div className="metric-value metric-positive">{hydrated ? activeCount : "—"}</div>
          <div className="metric-foot">Inactive rules stay on the list</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Persistence</div>
          <div className="metric-value">{PLANNER_PERSISTENCE_SEAM.kind}</div>
          <div className="metric-foot">{PLANNER_PERSISTENCE_SEAM.key}</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Execution</div>
          <div className="metric-value">Off</div>
          <div className="metric-foot">No venue credentials or order calls</div>
        </div>
      </section>

      <div className={styles.layout}>
        <section className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Paper watch rules</div>
              <div className="panel-meta">Create, edit, toggle and delete locally. Refresh keeps localStorage rules.</div>
            </div>
            <button className={styles.buttonGhost} type="button" onClick={resetToSeed}>
              Restore seed rules
            </button>
          </div>
          <div className="panel-body">
            {hydrated && rules.length === 0 ? (
              <div className={styles.empty}>No paper watch rules yet. Add one on the right. Nothing here can place a wager.</div>
            ) : (
              <div className={styles.ruleList}>
                {(hydrated ? rules : []).map((rule) => (
                  <WatchRuleCard
                    key={rule.id}
                    rule={rule}
                    onEdit={(next) => {
                      setDraft(draftFromRule(next));
                      setEditingId(next.id);
                    }}
                    onToggle={toggle}
                    onDelete={(id) => {
                      if (typeof window !== "undefined" && window.confirm("Delete this paper watch rule from local storage?")) {
                        remove(id);
                        if (editingId === id) {
                          setEditingId(null);
                          setDraft({ ...EMPTY_DRAFT });
                        }
                      }
                    }}
                  />
                ))}
              </div>
            )}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">{editingId ? "Edit paper rule" : "New paper rule"}</div>
              <div className="panel-meta">Planning fields only — no paper fill is submitted</div>
            </div>
          </div>
          <div className="panel-body">
            <WatchRuleForm
              draft={draft}
              editingId={editingId}
              onChange={setDraft}
              onSubmit={() => {
                upsert(draft);
                setEditingId(null);
                setDraft({ ...EMPTY_DRAFT });
              }}
              onCancel={() => setEditingId(null)}
            />
          </div>
        </section>
      </div>

      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Would-match preview</div>
            <div className="panel-meta">DEMO / FIXTURE DATA compared against your current paper rules</div>
          </div>
          <span className={styles.pillDemo}>DEMO / FIXTURE DATA</span>
        </div>
        <div className="panel-body">
          <DemoMatchPreview snapshots={DEMO_SCENARIO_SNAPSHOTS} rules={hydrated ? rules : []} />
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div className="panel-title">Backend seam</div>
          <div className="panel-meta">Not implemented in this UI-first slice</div>
        </div>
        <div className="panel-body">
          <p className={styles.seam}>
            Persistence is `{PLANNER_PERSISTENCE_SEAM.kind}` under `{PLANNER_PERSISTENCE_SEAM.key}`. A future paper
            execution service can replace `watchRuleStore` without adding bet placement here. Scenario Lab and team
            routes are linked for navigation; this page does not fetch live SRC series or fire on real markets.
          </p>
        </div>
      </section>
    </div>
  );
}
