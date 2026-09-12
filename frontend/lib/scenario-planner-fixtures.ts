/**
 * Scenario Planner — client-only paper watchlist fixtures and persistence seam.
 *
 * Phase 1 is PAPER MODE only. This module must never:
 * - place or cancel real bets
 * - call venue credentials / order APIs
 * - treat DEMO snapshots as live market triggers
 *
 * Persistence: browser localStorage. Replace `watchRuleStore` when a backend
 * paper-watch adapter exists. Execution integration is out of scope here.
 */

export const WATCH_RULES_STORAGE_KEY = "sports-hedge.scenario-planner.watch-rules.v2";

export const PLANNER_PERSISTENCE_SEAM = {
  kind: "localStorage" as const,
  key: WATCH_RULES_STORAGE_KEY,
  execution: "disabled" as const,
  note: "UI-first paper watchlist. Backend persistence and paper execution are future work.",
};

export type ResponseWindow = "0-5" | "0-10" | "0-15" | "0-30" | "remainder";
export type MarketFamily = "corners" | "cards" | "goals" | "match_result" | "btts";
export type RegimeRequirement = "current_manager" | "current_season" | "any_era";
export type DataQuality = "high" | "medium" | "low";
export type ScenarioMetric = "corners" | "yellow_cards" | "red_cards" | "goals";

export type ValueState = "VALUE" | "NO_VALUE" | "MISSING_COSTS" | "INSUFFICIENT_EVIDENCE";

export type PaperWatchRule = {
  id: string;
  active: boolean;
  teamId: string;
  teamName: string;
  scenarioId: string;
  metric: ScenarioMetric;
  responseWindow: ResponseWindow;
  competition: string;
  regimeRequirement: RegimeRequirement;
  minSrc: number;
  minSampleSize: number;
  minConfidence: number;
  minDataQuality: DataQuality;
  marketFamily: MarketFamily;
  minProbabilityEdgePp: number;
  minEvPerPound: number;
  maxQuoteAgeMinutes: number;
  requireKnownFees: boolean;
  paperStakeCapGbp: number | null;
  bankrollPercent: number | null;
  notes: string;
  createdAt: string;
  updatedAt: string;
};

export type DemoScenarioSnapshot = {
  id: string;
  demoLabel: string;
  teamId: string;
  teamName: string;
  opponent: string;
  scenarioId: string;
  metric: ScenarioMetric;
  responseWindow: ResponseWindow;
  competition: string;
  currentManager: boolean;
  currentSeason: boolean;
  src: number;
  sampleSize: number;
  confidence: number;
  dataQuality: DataQuality;
  marketFamily: MarketFamily;
  matchMinute: number;
  scoreline: string;
  modelProbability: number;
  bestNetOdds: number | null;
  impliedProbability: number | null;
  probabilityEdgePp: number | null;
  evPerPound: number | null;
  quoteAgeMinutes: number;
  feesKnown: boolean;
  valueState: ValueState;
};

export const TEAMS = [
  { id: "arsenal", name: "Arsenal" },
  { id: "chelsea", name: "Chelsea" },
  { id: "liverpool", name: "Liverpool" },
  { id: "manchester-city", name: "Manchester City" },
  { id: "newcastle", name: "Newcastle United" },
  { id: "tottenham", name: "Tottenham Hotspur" },
] as const;

export const SCENARIOS = [
  { id: "favourite-trails-underdog", label: "Favourite trails underdog" },
  { id: "favourite-concedes-first", label: "Favourite concedes first" },
  { id: "favourite-trailing-30", label: "Favourite trailing after 30 minutes" },
  { id: "favourite-trailing-70", label: "Favourite trailing after 70 minutes" },
  { id: "underdog-takes-lead", label: "Underdog takes the lead" },
  { id: "drawing-after-70", label: "Team drawing after 70 minutes" },
  { id: "leading-one-after-70", label: "Leading by one after 70 minutes" },
  { id: "opponent-red-while-trailing", label: "Opponent red card while trailing" },
  { id: "early-goal-conceded", label: "Early goal conceded" },
  { id: "late-equaliser", label: "Late equaliser" },
  { id: "defender-booked-early", label: "Defender booked early" },
  { id: "post-ucl-short-rest", label: "Post-Champions League / short rest" },
] as const;

export const METRICS: { id: ScenarioMetric; label: string }[] = [
  { id: "corners", label: "Corners" },
  { id: "yellow_cards", label: "Yellow cards" },
  { id: "red_cards", label: "Red cards" },
  { id: "goals", label: "Goals" },
];

export const RESPONSE_WINDOWS: { id: ResponseWindow; label: string }[] = [
  { id: "0-5", label: "Next 5 minutes" },
  { id: "0-10", label: "Next 10 minutes" },
  { id: "0-15", label: "Next 15 minutes" },
  { id: "0-30", label: "Next 30 minutes" },
  { id: "remainder", label: "Remainder of match" },
];

export const COMPETITIONS = [
  { id: "premier-league", label: "Premier League" },
  { id: "championship", label: "Championship" },
  { id: "la_liga", label: "La Liga" },
  { id: "serie_a", label: "Serie A" },
  { id: "bundesliga", label: "Bundesliga" },
  { id: "all", label: "Any competition" },
] as const;

export const REGIME_REQUIREMENTS: { id: RegimeRequirement; label: string }[] = [
  { id: "current_manager", label: "Current manager only" },
  { id: "current_season", label: "Current season only" },
  { id: "any_era", label: "Any manager era" },
];

export const DATA_QUALITIES: { id: DataQuality; label: string }[] = [
  { id: "high", label: "High" },
  { id: "medium", label: "Medium" },
  { id: "low", label: "Low (allow thin samples)" },
];

export const MARKET_FAMILIES: { id: MarketFamily; label: string }[] = [
  { id: "corners", label: "Corners" },
  { id: "cards", label: "Cards" },
  { id: "goals", label: "Goals" },
  { id: "match_result", label: "1X2 / match result" },
  { id: "btts", label: "Both teams to score" },
];

function lookup<T extends { id: string; label: string }>(items: readonly T[], id: string): string {
  return items.find((item) => item.id === id)?.label ?? id;
}

export function scenarioLabel(id: string): string {
  return lookup(SCENARIOS, id);
}

export function metricLabel(id: string): string {
  return lookup(METRICS, id);
}

export function windowLabel(id: string): string {
  return lookup(RESPONSE_WINDOWS, id);
}

export function competitionLabel(id: string): string {
  return lookup(COMPETITIONS, id);
}

export function regimeLabel(id: string): string {
  return lookup(REGIME_REQUIREMENTS, id);
}

export function marketFamilyLabel(id: string): string {
  return lookup(MARKET_FAMILIES, id);
}

export function summarizeRule(rule: PaperWatchRule): string {
  const src = `SRC >= ${rule.minSrc.toFixed(2)}`;
  const sample = `N >= ${rule.minSampleSize}`;
  const value = `edge >= ${rule.minProbabilityEdgePp.toFixed(1)}pp · EV/£1 >= ${rule.minEvPerPound.toFixed(3)}`;
  const regime = regimeLabel(rule.regimeRequirement).toLowerCase();
  return `${rule.teamName} — ${scenarioLabel(rule.scenarioId).toLowerCase()} — ${metricLabel(rule.metric).toLowerCase()} ${windowLabel(rule.responseWindow).toLowerCase()} — ${src} — ${sample} — ${value} — ${regime}.`;
}

const QUALITY_RANK: Record<DataQuality, number> = { low: 1, medium: 2, high: 3 };

export function ruleWouldMatch(rule: PaperWatchRule, snapshot: DemoScenarioSnapshot): boolean {
  if (!rule.active) return false;
  if (rule.teamId !== snapshot.teamId) return false;
  if (rule.scenarioId !== snapshot.scenarioId) return false;
  if (rule.metric !== snapshot.metric) return false;
  if (rule.responseWindow !== snapshot.responseWindow) return false;
  if (rule.competition !== "all" && rule.competition !== snapshot.competition) return false;
  if (rule.regimeRequirement === "current_manager" && !snapshot.currentManager) return false;
  if (rule.regimeRequirement === "current_season" && !snapshot.currentSeason) return false;
  if (snapshot.src < rule.minSrc) return false;
  if (snapshot.sampleSize < rule.minSampleSize) return false;
  if (snapshot.confidence < rule.minConfidence) return false;
  if (QUALITY_RANK[snapshot.dataQuality] < QUALITY_RANK[rule.minDataQuality]) return false;
  if (rule.marketFamily !== snapshot.marketFamily) return false;
  if (rule.requireKnownFees && !snapshot.feesKnown) return false;
  if (snapshot.quoteAgeMinutes > rule.maxQuoteAgeMinutes) return false;
  if (snapshot.probabilityEdgePp === null || snapshot.probabilityEdgePp < rule.minProbabilityEdgePp) return false;
  if (snapshot.evPerPound === null || snapshot.evPerPound < rule.minEvPerPound) return false;
  if (snapshot.valueState !== "VALUE") return false;
  return true;
}

export const SEED_WATCH_RULES: PaperWatchRule[] = [
  {
    id: "seed-arsenal-fav-trails-corners",
    active: true,
    teamId: "arsenal",
    teamName: "Arsenal",
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
    notes: "Paper watch only. Favourite trails an underdog; watch corners over the next 15 minutes under the current manager.",
    createdAt: "2026-09-11T12:00:00.000Z",
    updatedAt: "2026-09-11T12:00:00.000Z",
  },
  {
    id: "seed-liverpool-red-goals",
    active: false,
    teamId: "liverpool",
    teamName: "Liverpool",
    scenarioId: "opponent-red-while-trailing",
    metric: "goals",
    responseWindow: "0-30",
    competition: "premier-league",
    regimeRequirement: "current_season",
    minSrc: 0.45,
    minSampleSize: 12,
    minConfidence: 0.55,
    minDataQuality: "low",
    marketFamily: "goals",
    minProbabilityEdgePp: 1,
    minEvPerPound: 0.01,
    maxQuoteAgeMinutes: 15,
    requireKnownFees: true,
    paperStakeCapGbp: 100,
    bankrollPercent: 1,
    notes: "Inactive example. Kept local so the watchlist can be toggled without implying a live order.",
    createdAt: "2026-09-11T12:00:00.000Z",
    updatedAt: "2026-09-11T12:00:00.000Z",
  },
];

export const DEMO_SCENARIO_SNAPSHOTS: DemoScenarioSnapshot[] = [
  {
    id: "demo-arsenal-trails-corners",
    demoLabel: "DEMO / FIXTURE DATA — not a live market",
    teamId: "arsenal",
    teamName: "Arsenal",
    opponent: "Fulham",
    scenarioId: "favourite-trails-underdog",
    metric: "corners",
    responseWindow: "0-15",
    competition: "premier-league",
    currentManager: true,
    currentSeason: true,
    src: 0.72,
    sampleSize: 28,
    confidence: 0.81,
    dataQuality: "high",
    marketFamily: "corners",
    matchMinute: 32,
    scoreline: "0–1",
    modelProbability: 0.542,
    bestNetOdds: 2.156,
    impliedProbability: 0.464,
    probabilityEdgePp: 7.8,
    evPerPound: 0.169,
    quoteAgeMinutes: 4,
    feesKnown: true,
    valueState: "VALUE",
  },
  {
    id: "demo-arsenal-trails-thin-sample",
    demoLabel: "DEMO / FIXTURE DATA — high SRC but NO_VALUE once the market is priced",
    teamId: "arsenal",
    teamName: "Arsenal",
    opponent: "Fulham",
    scenarioId: "favourite-trails-underdog",
    metric: "corners",
    responseWindow: "0-15",
    competition: "premier-league",
    currentManager: true,
    currentSeason: true,
    src: 1.41,
    sampleSize: 47,
    confidence: 0.79,
    dataQuality: "low",
    marketFamily: "corners",
    matchMinute: 28,
    scoreline: "0–1",
    modelProbability: 0.49,
    bestNetOdds: 1.686,
    impliedProbability: 0.593,
    probabilityEdgePp: -10.3,
    evPerPound: -0.174,
    quoteAgeMinutes: 3,
    feesKnown: true,
    valueState: "NO_VALUE",
  },
  {
    id: "demo-newcastle-early-goal",
    demoLabel: "DEMO / FIXTURE DATA — different team and scenario",
    teamId: "newcastle",
    teamName: "Newcastle United",
    opponent: "Wolves",
    scenarioId: "early-goal-conceded",
    metric: "yellow_cards",
    responseWindow: "0-10",
    competition: "premier-league",
    currentManager: true,
    currentSeason: true,
    src: 0.58,
    sampleSize: 22,
    confidence: 0.76,
    dataQuality: "medium",
    marketFamily: "cards",
    matchMinute: 11,
    scoreline: "0–1",
    modelProbability: 0.4,
    bestNetOdds: null,
    impliedProbability: null,
    probabilityEdgePp: null,
    evPerPound: null,
    quoteAgeMinutes: 6,
    feesKnown: false,
    valueState: "MISSING_COSTS",
  },
];

export type WatchRuleStore = {
  load(): PaperWatchRule[] | null;
  save(rules: PaperWatchRule[]): void;
};

function isWatchRule(value: unknown): value is PaperWatchRule {
  if (!value || typeof value !== "object") return false;
  const rule = value as PaperWatchRule;
  return typeof rule.id === "string" && typeof rule.teamId === "string" && typeof rule.scenarioId === "string";
}

export const localStorageWatchRuleStore: WatchRuleStore = {
  load() {
    if (typeof window === "undefined") return null;
    try {
      const raw = window.localStorage.getItem(WATCH_RULES_STORAGE_KEY);
      if (!raw) return null;
      const parsed: unknown = JSON.parse(raw);
      if (!Array.isArray(parsed)) return null;
      const rules = parsed.filter(isWatchRule);
      return rules.length ? rules : null;
    } catch {
      return null;
    }
  },
  save(rules) {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(WATCH_RULES_STORAGE_KEY, JSON.stringify(rules));
  },
};

/** Explicit seam: swap this for a backend adapter later. Do not add order calls here. */
export const watchRuleStore: WatchRuleStore = localStorageWatchRuleStore;
