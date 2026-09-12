/**
 * DEMO / FIXTURE DATA for Scenario Lab.
 * Values are illustrative product-design placeholders, not historical findings.
 */

export const SCENARIO_DATA_LABEL = "DEMO / FIXTURE DATA";

export type ResponseWindow = "5m" | "10m" | "15m" | "30m" | "rest";
export type MetricId = "corners" | "yellow_cards" | "red_cards" | "goals";
export type VenueFilter = "all" | "home" | "away";
export type ManagerEraFilter = "current" | "previous" | "all";
export type SeasonPhaseFilter = "all" | "OPENING_10" | "MID_SEASON" | "RUN_IN";
export type Stability = "High" | "Medium" | "Low";
export type DataQuality = "Good" | "Thin" | "Mixed regime";

export type ScenarioDefinition = {
  id: string;
  label: string;
  trigger: string;
  description: string;
};

export type TeamRecord = {
  teamId: string;
  name: string;
  short: string;
  competition: string;
  currentManager: string;
  previousManager: string;
  currentRegimeMatches: number;
};

export type TimeCurvePoint = {
  window: ResponseWindow;
  label: string;
  teamEffect: number;
  leagueEffect: number;
};

export type ManagerSplit = {
  era: "current" | "previous";
  manager: string;
  sampleN: number;
  teamEffect: number;
  excessResponse: number;
  src: number;
};

export type HistoricalMatch = {
  matchId: string;
  teamId: string;
  scenarioId: string;
  date: string;
  fixture: string;
  venue: "home" | "away";
  scoreAtTrigger: string;
  minute: number;
  metricValue: number;
  manager: string;
  seasonPhase: Exclude<SeasonPhaseFilter, "all">;
  regime: "current" | "previous";
};

export type RankingRow = {
  teamId: string;
  teamName: string;
  scenarioId: string;
  metric: MetricId;
  window: ResponseWindow;
  competition: string;
  venue: VenueFilter;
  managerEra: ManagerEraFilter;
  seasonPhase: SeasonPhaseFilter;
  src: number;
  sri: number;
  teamBaseline: number;
  teamScenarioValue: number;
  teamEffect: number;
  leagueBaseline: number;
  leagueScenarioValue: number;
  leagueEffect: number;
  excessResponse: number;
  sampleN: number;
  confidence: number;
  stability: Stability;
  dataQuality: DataQuality;
  regimeMix: boolean;
  currentManagerN: number;
  previousManagerN: number;
  fullTeamN: number;
};

export type ScenarioFilters = {
  scenarioId: string;
  metric: MetricId;
  window: ResponseWindow;
  competition: string;
  teamId: string;
  venue: VenueFilter;
  managerEra: ManagerEraFilter;
  seasonPhase: SeasonPhaseFilter;
  minSample: number;
  minConfidence: number;
};

export const WINDOW_OPTIONS: { id: ResponseWindow; label: string }[] = [
  { id: "5m", label: "0–5m" },
  { id: "10m", label: "0–10m" },
  { id: "15m", label: "0–15m" },
  { id: "30m", label: "0–30m" },
  { id: "rest", label: "Rest of match" },
];

export const METRIC_OPTIONS: { id: MetricId; label: string }[] = [
  { id: "corners", label: "Corners" },
  { id: "yellow_cards", label: "Yellow cards" },
  { id: "red_cards", label: "Red cards" },
  { id: "goals", label: "Goals" },
];

export const COMPETITION_OPTIONS = ["Premier League"] as const;

export const scenarios: ScenarioDefinition[] = [
  {
    id: "fav-trails-underdog",
    label: "Favourite trails underdog",
    trigger: "CONCEDED_GOAL · favourite · underdog opponent",
    description: "Pre-match favourite falls behind against an underdog. Headline demo: corners in the next 15 minutes.",
  },
  {
    id: "favourite-concedes-first",
    label: "Favourite concedes first",
    trigger: "CONCEDED_GOAL · first goal · favourite",
    description: "Favourite concedes the opening goal, regardless of later score state.",
  },
  {
    id: "trailing-after-70",
    label: "Favourite trailing after 70m",
    trigger: "SCORE_STATE · minute ≥ 70 · favourite trailing",
    description: "Favourite still behind entering the last 20 minutes.",
  },
  {
    id: "underdog-takes-lead",
    label: "Underdog takes the lead",
    trigger: "GOAL · underdog · leads",
    description: "Underdog scores to go ahead; used as a contrast scenario in the demo set.",
  },
  {
    id: "drawing-after-70",
    label: "Team drawing after 70m",
    trigger: "SCORE_STATE · minute ≥ 70 · level",
    description: "Level late; typically a lower-intensity set-piece response than chasing a deficit.",
  },
  {
    id: "opponent-red-trailing",
    label: "Opponent red card while trailing",
    trigger: "OPPONENT_RED_CARD · trailing",
    description: "Numerical advantage while chasing; thin-sample demo rows are expected.",
  },
];

export const teams: TeamRecord[] = [
  { teamId: "arsenal", name: "Arsenal", short: "ARS", competition: "Premier League", currentManager: "Mikel Arteta", previousManager: "Unai Emery", currentRegimeMatches: 214 },
  { teamId: "manchester-city", name: "Manchester City", short: "MCI", competition: "Premier League", currentManager: "Pep Guardiola", previousManager: "Manuel Pellegrini", currentRegimeMatches: 380 },
  { teamId: "liverpool", name: "Liverpool", short: "LIV", competition: "Premier League", currentManager: "Arne Slot", previousManager: "Jürgen Klopp", currentRegimeMatches: 48 },
  { teamId: "brighton", name: "Brighton & Hove Albion", short: "BHA", competition: "Premier League", currentManager: "Fabian Hürzeler", previousManager: "Roberto De Zerbi", currentRegimeMatches: 36 },
  { teamId: "chelsea", name: "Chelsea", short: "CHE", competition: "Premier League", currentManager: "Enzo Maresca", previousManager: "Mauricio Pochettino", currentRegimeMatches: 42 },
  { teamId: "tottenham", name: "Tottenham Hotspur", short: "TOT", competition: "Premier League", currentManager: "Thomas Frank", previousManager: "Ange Postecoglou", currentRegimeMatches: 8 },
  { teamId: "newcastle", name: "Newcastle United", short: "NEW", competition: "Premier League", currentManager: "Eddie Howe", previousManager: "Steve Bruce", currentRegimeMatches: 160 },
  { teamId: "fulham", name: "Fulham", short: "FUL", competition: "Premier League", currentManager: "Marco Silva", previousManager: "Scott Parker", currentRegimeMatches: 140 },
  { teamId: "everton", name: "Everton", short: "EVE", competition: "Premier League", currentManager: "David Moyes", previousManager: "Sean Dyche", currentRegimeMatches: 22 },
  { teamId: "wolves", name: "Wolverhampton Wanderers", short: "WOL", competition: "Premier League", currentManager: "Vítor Pereira", previousManager: "Gary O'Neil", currentRegimeMatches: 18 },
  { teamId: "west-ham", name: "West Ham United", short: "WHU", competition: "Premier League", currentManager: "Graham Potter", previousManager: "Julen Lopetegui", currentRegimeMatches: 28 },
];

type SeedMetric = {
  teamBaseline: number;
  teamScenario: number;
  leagueBaseline: number;
  leagueScenario: number;
  nCurrent: number;
  nPrevious: number;
  confidence: number;
  stability: Stability;
};

type TeamSeed = {
  teamId: string;
  corners: SeedMetric;
  yellow_cards: SeedMetric;
  red_cards: SeedMetric;
  goals: SeedMetric;
};

const WINDOW_SCALE: Record<ResponseWindow, number> = {
  "5m": 0.36,
  "10m": 0.67,
  "15m": 1,
  "30m": 1.26,
  "rest": 1.58,
};

const VENUE_SCALE: Record<VenueFilter, number> = { all: 1, home: 1.11, away: 0.89 };
const PHASE_SCALE: Record<SeasonPhaseFilter, number> = {
  all: 1,
  OPENING_10: 0.84,
  MID_SEASON: 1.03,
  RUN_IN: 1.09,
};

const SCENARIO_INTENSITY: Record<string, number> = {
  "fav-trails-underdog": 1,
  "favourite-concedes-first": 0.92,
  "trailing-after-70": 1.08,
  "underdog-takes-lead": 0.88,
  "drawing-after-70": 0.54,
  "opponent-red-trailing": 1.22,
};

const teamSeeds: TeamSeed[] = [
  {
    teamId: "arsenal",
    corners: { teamBaseline: 1.08, teamScenario: 2.49, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 28, nPrevious: 19, confidence: 0.88, stability: "High" },
    yellow_cards: { teamBaseline: 0.31, teamScenario: 0.42, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 28, nPrevious: 19, confidence: 0.81, stability: "High" },
    red_cards: { teamBaseline: 0.02, teamScenario: 0.03, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 28, nPrevious: 19, confidence: 0.41, stability: "Low" },
    goals: { teamBaseline: 0.22, teamScenario: 0.41, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 28, nPrevious: 19, confidence: 0.79, stability: "Medium" },
  },
  {
    teamId: "manchester-city",
    corners: { teamBaseline: 1.21, teamScenario: 2.31, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 24, nPrevious: 16, confidence: 0.84, stability: "High" },
    yellow_cards: { teamBaseline: 0.24, teamScenario: 0.29, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 24, nPrevious: 16, confidence: 0.77, stability: "Medium" },
    red_cards: { teamBaseline: 0.01, teamScenario: 0.02, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 24, nPrevious: 16, confidence: 0.38, stability: "Low" },
    goals: { teamBaseline: 0.28, teamScenario: 0.52, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 24, nPrevious: 16, confidence: 0.82, stability: "High" },
  },
  {
    teamId: "liverpool",
    corners: { teamBaseline: 0.99, teamScenario: 2.02, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 11, nPrevious: 26, confidence: 0.71, stability: "Medium" },
    yellow_cards: { teamBaseline: 0.29, teamScenario: 0.38, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 11, nPrevious: 26, confidence: 0.66, stability: "Medium" },
    red_cards: { teamBaseline: 0.02, teamScenario: 0.03, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 11, nPrevious: 26, confidence: 0.34, stability: "Low" },
    goals: { teamBaseline: 0.24, teamScenario: 0.39, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 11, nPrevious: 26, confidence: 0.69, stability: "Medium" },
  },
  {
    teamId: "brighton",
    corners: { teamBaseline: 0.94, teamScenario: 2.18, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 8, nPrevious: 14, confidence: 0.52, stability: "Low" },
    yellow_cards: { teamBaseline: 0.34, teamScenario: 0.51, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 8, nPrevious: 14, confidence: 0.48, stability: "Low" },
    red_cards: { teamBaseline: 0.03, teamScenario: 0.05, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 8, nPrevious: 14, confidence: 0.29, stability: "Low" },
    goals: { teamBaseline: 0.19, teamScenario: 0.27, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 8, nPrevious: 14, confidence: 0.47, stability: "Low" },
  },
  {
    teamId: "chelsea",
    corners: { teamBaseline: 0.91, teamScenario: 1.96, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 6, nPrevious: 31, confidence: 0.58, stability: "Low" },
    yellow_cards: { teamBaseline: 0.36, teamScenario: 0.55, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 6, nPrevious: 31, confidence: 0.55, stability: "Low" },
    red_cards: { teamBaseline: 0.03, teamScenario: 0.04, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 6, nPrevious: 31, confidence: 0.31, stability: "Low" },
    goals: { teamBaseline: 0.2, teamScenario: 0.31, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 6, nPrevious: 31, confidence: 0.53, stability: "Low" },
  },
  {
    teamId: "tottenham",
    corners: { teamBaseline: 0.88, teamScenario: 1.49, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 4, nPrevious: 21, confidence: 0.44, stability: "Low" },
    yellow_cards: { teamBaseline: 0.32, teamScenario: 0.46, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 4, nPrevious: 21, confidence: 0.42, stability: "Low" },
    red_cards: { teamBaseline: 0.02, teamScenario: 0.04, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 4, nPrevious: 21, confidence: 0.27, stability: "Low" },
    goals: { teamBaseline: 0.21, teamScenario: 0.28, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 4, nPrevious: 21, confidence: 0.41, stability: "Low" },
  },
  {
    teamId: "newcastle",
    corners: { teamBaseline: 0.83, teamScenario: 1.71, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 22, nPrevious: 11, confidence: 0.8, stability: "High" },
    yellow_cards: { teamBaseline: 0.35, teamScenario: 0.5, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 22, nPrevious: 11, confidence: 0.76, stability: "Medium" },
    red_cards: { teamBaseline: 0.03, teamScenario: 0.05, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 22, nPrevious: 11, confidence: 0.4, stability: "Low" },
    goals: { teamBaseline: 0.17, teamScenario: 0.26, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 22, nPrevious: 11, confidence: 0.74, stability: "Medium" },
  },
  {
    teamId: "fulham",
    corners: { teamBaseline: 0.79, teamScenario: 1.55, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 18, nPrevious: 12, confidence: 0.76, stability: "Medium" },
    yellow_cards: { teamBaseline: 0.3, teamScenario: 0.44, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 18, nPrevious: 12, confidence: 0.73, stability: "Medium" },
    red_cards: { teamBaseline: 0.02, teamScenario: 0.03, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 18, nPrevious: 12, confidence: 0.36, stability: "Low" },
    goals: { teamBaseline: 0.16, teamScenario: 0.24, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 18, nPrevious: 12, confidence: 0.71, stability: "Medium" },
  },
  {
    teamId: "everton",
    corners: { teamBaseline: 0.71, teamScenario: 1.22, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 9, nPrevious: 20, confidence: 0.61, stability: "Medium" },
    yellow_cards: { teamBaseline: 0.38, teamScenario: 0.57, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 9, nPrevious: 20, confidence: 0.63, stability: "Medium" },
    red_cards: { teamBaseline: 0.03, teamScenario: 0.06, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 9, nPrevious: 20, confidence: 0.33, stability: "Low" },
    goals: { teamBaseline: 0.14, teamScenario: 0.2, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 9, nPrevious: 20, confidence: 0.58, stability: "Medium" },
  },
  {
    teamId: "wolves",
    corners: { teamBaseline: 0.66, teamScenario: 0.93, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 7, nPrevious: 16, confidence: 0.57, stability: "Medium" },
    yellow_cards: { teamBaseline: 0.4, teamScenario: 0.61, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 7, nPrevious: 16, confidence: 0.59, stability: "Medium" },
    red_cards: { teamBaseline: 0.04, teamScenario: 0.07, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 7, nPrevious: 16, confidence: 0.31, stability: "Low" },
    goals: { teamBaseline: 0.13, teamScenario: 0.17, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 7, nPrevious: 16, confidence: 0.55, stability: "Low" },
  },
  {
    teamId: "west-ham",
    corners: { teamBaseline: 0.74, teamScenario: 1.18, leagueBaseline: 0.86, leagueScenario: 1.58, nCurrent: 10, nPrevious: 17, confidence: 0.64, stability: "Medium" },
    yellow_cards: { teamBaseline: 0.37, teamScenario: 0.52, leagueBaseline: 0.33, leagueScenario: 0.47, nCurrent: 10, nPrevious: 17, confidence: 0.62, stability: "Medium" },
    red_cards: { teamBaseline: 0.03, teamScenario: 0.05, leagueBaseline: 0.02, leagueScenario: 0.04, nCurrent: 10, nPrevious: 17, confidence: 0.32, stability: "Low" },
    goals: { teamBaseline: 0.15, teamScenario: 0.21, leagueBaseline: 0.18, leagueScenario: 0.29, nCurrent: 10, nPrevious: 17, confidence: 0.6, stability: "Medium" },
  },
];

const PREVIOUS_ERA_RATIO: Record<string, number> = {
  arsenal: 0.41,
  "manchester-city": 0.78,
  liverpool: 1.12,
  brighton: 0.86,
  chelsea: 0.38,
  tottenham: 0.71,
  newcastle: 0.62,
  fulham: 0.81,
  everton: 0.69,
  wolves: 0.84,
  "west-ham": 0.73,
};

function round(value: number, digits = 2): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function stabilityWeight(stability: Stability): number {
  if (stability === "High") return 1;
  if (stability === "Medium") return 0.82;
  return 0.64;
}

function sampleFor(era: ManagerEraFilter, nCurrent: number, nPrevious: number, venue: VenueFilter, phase: SeasonPhaseFilter): number {
  const eraN = era === "current" ? nCurrent : era === "previous" ? nPrevious : nCurrent + nPrevious;
  const venueFactor = venue === "all" ? 1 : 0.58;
  const phaseFactor = phase === "all" ? 1 : phase === "MID_SEASON" ? 0.55 : 0.28;
  return Math.max(1, Math.round(eraN * venueFactor * phaseFactor));
}

function dataQuality(sampleN: number, regimeMix: boolean, confidence: number): DataQuality {
  if (regimeMix) return "Mixed regime";
  if (sampleN < 12 || confidence < 0.6) return "Thin";
  return "Good";
}

function buildRow(args: {
  team: TeamRecord;
  seed: SeedMetric;
  scenarioId: string;
  metric: MetricId;
  window: ResponseWindow;
  venue: VenueFilter;
  managerEra: ManagerEraFilter;
  seasonPhase: SeasonPhaseFilter;
}): RankingRow {
  const intensity = SCENARIO_INTENSITY[args.scenarioId] ?? 1;
  const scale = WINDOW_SCALE[args.window] * VENUE_SCALE[args.venue] * PHASE_SCALE[args.seasonPhase] * intensity;
  const previousRatio = PREVIOUS_ERA_RATIO[args.team.teamId] ?? 0.75;
  const eraRatio = args.managerEra === "previous" ? previousRatio : args.managerEra === "all" ? (1 + previousRatio) / 2 : 1;

  const teamBaseline = round(args.seed.teamBaseline * (args.window === "5m" ? 0.42 : args.window === "10m" ? 0.7 : args.window === "15m" ? 1 : args.window === "30m" ? 1.35 : 1.9));
  const leagueBaseline = round(args.seed.leagueBaseline * (args.window === "5m" ? 0.42 : args.window === "10m" ? 0.7 : args.window === "15m" ? 1 : args.window === "30m" ? 1.35 : 1.9));
  const teamScenarioValue = round(args.seed.teamBaseline + (args.seed.teamScenario - args.seed.teamBaseline) * scale * eraRatio);
  const leagueScenarioValue = round(args.seed.leagueBaseline + (args.seed.leagueScenario - args.seed.leagueBaseline) * WINDOW_SCALE[args.window] * intensity);
  const teamEffect = round(teamScenarioValue - teamBaseline);
  const leagueEffect = round(leagueScenarioValue - leagueBaseline);
  const excessResponse = round(teamEffect - leagueEffect);
  const src = round(excessResponse / Math.max(0.18, Math.abs(leagueEffect)));
  const confidence = round(
    clamp(
      args.seed.confidence * (args.managerEra === "current" ? 1 : args.managerEra === "previous" ? 0.92 : 0.84) * (args.venue === "all" ? 1 : 0.9),
      0.2,
      0.95,
    ),
    2,
  );
  const sampleN = sampleFor(args.managerEra, args.seed.nCurrent, args.seed.nPrevious, args.venue, args.seasonPhase);
  const regimeMix = args.managerEra === "all" && args.seed.nCurrent < 20 && Math.abs(1 - previousRatio) > 0.25;
  const sri = Math.round(
    clamp(50 + src * 22 * (0.45 + 0.55 * confidence) * stabilityWeight(args.seed.stability), 4, 97),
  );

  return {
    teamId: args.team.teamId,
    teamName: args.team.name,
    scenarioId: args.scenarioId,
    metric: args.metric,
    window: args.window,
    competition: args.team.competition,
    venue: args.venue,
    managerEra: args.managerEra,
    seasonPhase: args.seasonPhase,
    src,
    sri,
    teamBaseline,
    teamScenarioValue,
    teamEffect,
    leagueBaseline,
    leagueScenarioValue,
    leagueEffect,
    excessResponse,
    sampleN,
    confidence,
    stability: args.seed.stability,
    dataQuality: dataQuality(sampleN, regimeMix, confidence),
    regimeMix,
    currentManagerN: args.seed.nCurrent,
    previousManagerN: args.seed.nPrevious,
    fullTeamN: args.seed.nCurrent + args.seed.nPrevious,
  };
}

function rowFor(filters: Pick<ScenarioFilters, "scenarioId" | "metric" | "window" | "venue" | "managerEra" | "seasonPhase">, team: TeamRecord): RankingRow | null {
  const seedBundle = teamSeeds.find((item) => item.teamId === team.teamId);
  if (!seedBundle) return null;
  return buildRow({
    team,
    seed: seedBundle[filters.metric],
    scenarioId: filters.scenarioId,
    metric: filters.metric,
    window: filters.window,
    venue: filters.venue,
    managerEra: filters.managerEra,
    seasonPhase: filters.seasonPhase,
  });
}

export const defaultFilters: ScenarioFilters = {
  scenarioId: "fav-trails-underdog",
  metric: "corners",
  window: "15m",
  competition: "Premier League",
  teamId: "all",
  venue: "all",
  managerEra: "current",
  seasonPhase: "all",
  minSample: 10,
  minConfidence: 0.5,
};

export function getScenario(scenarioId: string): ScenarioDefinition {
  return scenarios.find((item) => item.id === scenarioId) ?? scenarios[0];
}

export function getTeam(teamId: string): TeamRecord | undefined {
  return teams.find((item) => item.teamId === teamId);
}

export function filterRanking(filters: ScenarioFilters): RankingRow[] {
  return teams
    .filter((team) => team.competition === filters.competition)
    .filter((team) => filters.teamId === "all" || team.teamId === filters.teamId)
    .map((team) => rowFor(filters, team))
    .filter((row): row is RankingRow => row !== null)
    .filter((row) => row.sampleN >= filters.minSample)
    .filter((row) => row.confidence >= filters.minConfidence)
    .sort((a, b) => b.src - a.src);
}

export function timeResponseCurve(filters: ScenarioFilters, teamId: string): TimeCurvePoint[] {
  const team = getTeam(teamId);
  if (!team) return [];
  return WINDOW_OPTIONS.map((window) => {
    const row = rowFor({ ...filters, window: window.id }, team);
    return {
      window: window.id,
      label: window.label,
      teamEffect: row?.teamEffect ?? 0,
      leagueEffect: row?.leagueEffect ?? 0,
    };
  });
}

export function managerSplit(filters: ScenarioFilters, teamId: string): ManagerSplit[] {
  const team = getTeam(teamId);
  const current = team ? rowFor({ ...filters, managerEra: "current" }, team) : null;
  const previous = team ? rowFor({ ...filters, managerEra: "previous" }, team) : null;
  return [
    {
      era: "current",
      manager: team?.currentManager ?? "Current manager",
      sampleN: current?.sampleN ?? 0,
      teamEffect: current?.teamEffect ?? 0,
      excessResponse: current?.excessResponse ?? 0,
      src: current?.src ?? 0,
    },
    {
      era: "previous",
      manager: team?.previousManager ?? "Previous manager",
      sampleN: previous?.sampleN ?? 0,
      teamEffect: previous?.teamEffect ?? 0,
      excessResponse: previous?.excessResponse ?? 0,
      src: previous?.src ?? 0,
    },
  ];
}

export const historicalMatches: HistoricalMatch[] = [
  { matchId: "ars-avl-240414", teamId: "arsenal", scenarioId: "fav-trails-underdog", date: "2024-04-14", fixture: "Arsenal 0–2 Aston Villa", venue: "home", scoreAtTrigger: "0–1", minute: 14, metricValue: 4, manager: "Mikel Arteta", seasonPhase: "RUN_IN", regime: "current" },
  { matchId: "ars-whu-240204", teamId: "arsenal", scenarioId: "fav-trails-underdog", date: "2024-02-04", fixture: "West Ham 2–2 Arsenal", venue: "away", scoreAtTrigger: "1–0", minute: 16, metricValue: 3, manager: "Mikel Arteta", seasonPhase: "MID_SEASON", regime: "current" },
  { matchId: "ars-ful-231203", teamId: "arsenal", scenarioId: "fav-trails-underdog", date: "2023-12-03", fixture: "Arsenal 1–1 Fulham", venue: "home", scoreAtTrigger: "0–1", minute: 9, metricValue: 5, manager: "Mikel Arteta", seasonPhase: "MID_SEASON", regime: "current" },
  { matchId: "ars-wol-231102", teamId: "arsenal", scenarioId: "fav-trails-underdog", date: "2023-11-02", fixture: "Wolves 2–1 Arsenal", venue: "away", scoreAtTrigger: "1–0", minute: 22, metricValue: 2, manager: "Mikel Arteta", seasonPhase: "MID_SEASON", regime: "current" },
  { matchId: "ars-bou-240831", teamId: "arsenal", scenarioId: "fav-trails-underdog", date: "2024-08-31", fixture: "Arsenal 1–1 Brighton", venue: "home", scoreAtTrigger: "0–1", minute: 11, metricValue: 4, manager: "Mikel Arteta", seasonPhase: "OPENING_10", regime: "current" },
  { matchId: "ars-cry-190127", teamId: "arsenal", scenarioId: "fav-trails-underdog", date: "2019-01-27", fixture: "Arsenal 2–2 Cardiff", venue: "home", scoreAtTrigger: "0–1", minute: 7, metricValue: 1, manager: "Unai Emery", seasonPhase: "MID_SEASON", regime: "previous" },
  { matchId: "ars-lei-181022", teamId: "arsenal", scenarioId: "fav-trails-underdog", date: "2018-10-22", fixture: "Leicester 3–1 Arsenal", venue: "away", scoreAtTrigger: "1–0", minute: 31, metricValue: 1, manager: "Unai Emery", seasonPhase: "OPENING_10", regime: "previous" },
  { matchId: "mci-ful-240413", teamId: "manchester-city", scenarioId: "fav-trails-underdog", date: "2024-04-13", fixture: "Fulham 1–4 Manchester City", venue: "away", scoreAtTrigger: "1–0", minute: 18, metricValue: 3, manager: "Pep Guardiola", seasonPhase: "RUN_IN", regime: "current" },
  { matchId: "liv-nfo-240914", teamId: "liverpool", scenarioId: "fav-trails-underdog", date: "2024-09-14", fixture: "Liverpool 0–1 Nottingham Forest", venue: "home", scoreAtTrigger: "0–1", minute: 8, metricValue: 3, manager: "Arne Slot", seasonPhase: "OPENING_10", regime: "current" },
  { matchId: "che-bou-240919", teamId: "chelsea", scenarioId: "fav-trails-underdog", date: "2024-09-14", fixture: "Chelsea 1–1 Bournemouth", venue: "home", scoreAtTrigger: "0–1", minute: 12, metricValue: 2, manager: "Enzo Maresca", seasonPhase: "OPENING_10", regime: "current" },
  { matchId: "che-ast-240406", teamId: "chelsea", scenarioId: "fav-trails-underdog", date: "2024-04-27", fixture: "Aston Villa 2–2 Chelsea", venue: "away", scoreAtTrigger: "1–0", minute: 21, metricValue: 1, manager: "Mauricio Pochettino", seasonPhase: "RUN_IN", regime: "previous" },
  { matchId: "wol-ars-240424", teamId: "wolves", scenarioId: "fav-trails-underdog", date: "2024-04-24", fixture: "Wolves 0–2 Bournemouth", venue: "home", scoreAtTrigger: "0–1", minute: 19, metricValue: 0, manager: "Gary O'Neil", seasonPhase: "RUN_IN", regime: "previous" },
  { matchId: "ars-nfo-231012", teamId: "arsenal", scenarioId: "favourite-concedes-first", date: "2023-10-21", fixture: "Chelsea 2–2 Arsenal", venue: "away", scoreAtTrigger: "1–0", minute: 15, metricValue: 3, manager: "Mikel Arteta", seasonPhase: "OPENING_10", regime: "current" },
  { matchId: "ars-bou-240930", teamId: "arsenal", scenarioId: "favourite-concedes-first", date: "2024-10-19", fixture: "Arsenal 2–2 Bournemouth", venue: "home", scoreAtTrigger: "0–1", minute: 6, metricValue: 4, manager: "Mikel Arteta", seasonPhase: "OPENING_10", regime: "current" },
  { matchId: "ars-tot-240415", teamId: "arsenal", scenarioId: "trailing-after-70", date: "2023-09-24", fixture: "Arsenal 2–2 Tottenham", venue: "home", scoreAtTrigger: "0–1", minute: 71, metricValue: 2, manager: "Mikel Arteta", seasonPhase: "OPENING_10", regime: "current" },
];

export function matchesFor(filters: ScenarioFilters, teamId: string): HistoricalMatch[] {
  return historicalMatches
    .filter((match) => match.scenarioId === filters.scenarioId && match.teamId === teamId)
    .filter((match) => filters.venue === "all" || match.venue === filters.venue)
    .filter((match) => filters.managerEra === "all" || match.regime === filters.managerEra)
    .filter((match) => filters.seasonPhase === "all" || match.seasonPhase === filters.seasonPhase);
}

export function formatSigned(value: number, digits = 2): string {
  const formatted = value.toFixed(digits);
  return value > 0 ? `+${formatted}` : formatted;
}

export function formatPct(value: number): string {
  return `${Math.round(value * 100)}%`;
}
