"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  PaperWatchRule,
  SEED_WATCH_RULES,
  TEAMS,
  watchRuleStore,
} from "../../lib/scenario-planner-fixtures";

export type WatchRuleDraft = Omit<PaperWatchRule, "id" | "createdAt" | "updatedAt" | "teamName"> & {
  id?: string;
};

export const EMPTY_DRAFT: WatchRuleDraft = {
  active: true,
  teamId: "arsenal",
  scenarioId: "favourite-trails-underdog",
  metric: "corners",
  responseWindow: "0-15",
  competition: "premier-league",
  regimeRequirement: "current_manager",
  minSrc: 0.6,
  minSampleSize: 20,
  minConfidence: 0.7,
  minDataQuality: "medium",
  marketFamily: "corners",
  minProbabilityEdgePp: 2,
  minEvPerPound: 0.02,
  maxQuoteAgeMinutes: 10,
  requireKnownFees: true,
  paperStakeCapGbp: 250,
  bankrollPercent: 2,
  notes: "",
};

function teamNameFor(teamId: string): string {
  return TEAMS.find((team) => team.id === teamId)?.name ?? teamId;
}

function stamp(rule: WatchRuleDraft, existing?: PaperWatchRule): PaperWatchRule {
  const now = new Date().toISOString();
  return {
    ...rule,
    id: existing?.id ?? (typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : `rule-${Date.now()}`),
    teamName: teamNameFor(rule.teamId),
    createdAt: existing?.createdAt ?? now,
    updatedAt: now,
  };
}

export function usePaperWatchRules() {
  const [rules, setRules] = useState<PaperWatchRule[]>(SEED_WATCH_RULES);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    const stored = watchRuleStore.load();
    setRules(stored ?? SEED_WATCH_RULES);
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    watchRuleStore.save(rules);
  }, [hydrated, rules]);

  const upsert = useCallback((draft: WatchRuleDraft) => {
    setRules((current) => {
      const existing = draft.id ? current.find((rule) => rule.id === draft.id) : undefined;
      const next = stamp(draft, existing);
      if (!existing) return [next, ...current];
      return current.map((rule) => (rule.id === next.id ? next : rule));
    });
  }, []);

  const toggle = useCallback((id: string) => {
    setRules((current) =>
      current.map((rule) =>
        rule.id === id ? { ...rule, active: !rule.active, updatedAt: new Date().toISOString() } : rule,
      ),
    );
  }, []);

  const remove = useCallback((id: string) => {
    setRules((current) => current.filter((rule) => rule.id !== id));
  }, []);

  const resetToSeed = useCallback(() => {
    setRules(SEED_WATCH_RULES);
  }, []);

  const activeCount = useMemo(() => rules.filter((rule) => rule.active).length, [rules]);

  return { rules, hydrated, upsert, toggle, remove, resetToSeed, activeCount };
}
