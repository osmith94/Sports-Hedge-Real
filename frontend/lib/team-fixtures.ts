/**
 * Typed DEMO / FIXTURE DATA provider for Team Explorer.
 *
 * This module is an illustrative stand-in for future live APIs.
 * Values are invented for UI review. They are not historical observations,
 * production coefficients, or betting signals.
 */

export const DEMO_FIXTURE_BANNER = "DEMO / FIXTURE DATA";

export const DEMO_FIXTURE_DISCLAIMER =
  "DEMO / FIXTURE DATA — illustrative Scenario Response Profiles for UI review. These coefficients are not live observations, not historical production results, and must not be treated as betting signals.";

export type CompetitionId = "premier-league" | "championship" | "la-liga";
export type Metric = "corners" | "yellow_cards" | "red_cards" | "goals";
export type ResponseWindow = "0-5" | "0-10" | "0-15" | "0-30" | "remainder";
export type ProfileWindow = "last_30_days" | "current_season" | "current_manager";
export type DataQuality = "sufficient" | "limited" | "sparse";
export type ManagerPhase = "DEBUT" | "VERY_EARLY" | "EARLY" | "DEVELOPING" | "ESTABLISHED";
export type AppointmentType = "PERMANENT" | "INTERIM" | "CARETAKER";
export type Venue = "H" | "A";

export type Competition = {
  id: CompetitionId;
  name: string;
  country: string;
};

export const COMPETITIONS: Competition[] = [
  { id: "premier-league", name: "Premier League", country: "England" },
  { id: "championship", name: "Championship", country: "England" },
  { id: "la-liga", name: "La Liga", country: "Spain" },
];

export type TeamIdentity = {
  id: string;
  name: string;
  shortName: string;
  competitionId: CompetitionId;
  city: string;
  colours: string;
  srcIndex: number;
  primaryMetric: Metric;
  dataQuality: DataQuality;
  sampleSize: number;
};

export type NextFixture = {
  opponentId: string;
  opponentName: string;
  competitionId: CompetitionId;
  venue: Venue;
  kickoffIso: string;
  kickoffLabel: string;
  context: string;
  impliedFavourite: "home" | "away" | "even";
};

export type ManagerRegime = {
  managerId: string;
  managerName: string;
  appointmentType: AppointmentType;
  phase: ManagerPhase;
  regimeStart: string;
  matchesInRegime: number;
  isCurrent: boolean;
};

export type ScenarioResponse = {
  scenarioId: string;
  scenarioName: string;
  trigger: string;
  metric: Metric;
  responseWindow: ResponseWindow;
  profileWindow: ProfileWindow;
  teamBaseline: number;
  teamScenarioValue: number;
  teamEffect: number;
  leagueBaseline: number;
  leagueScenarioValue: number;
  leagueEffect: number;
  excessResponse: number;
  src: number;
  n: number;
  confidence: number;
  stability: number;
  dataQuality: DataQuality;
};

export type RegimeComparisonRow = {
  scenarioId: string;
  scenarioName: string;
  metric: Metric;
  responseWindow: ResponseWindow;
  currentSrc: number;
  previousSrc: number;
  currentN: number;
  previousN: number;
  currentManager: string;
  previousManager: string;
};

export type TeamDashboard = {
  team: TeamIdentity;
  nextFixture: NextFixture;
  currentManager: ManagerRegime;
  previousManager: ManagerRegime | null;
  profiles: Record<ProfileWindow, ScenarioResponse[]>;
  regimeComparison: RegimeComparisonRow[];
  notes: string[];
};

export const PROFILE_WINDOWS: { id: ProfileWindow; label: string }[] = [
  { id: "last_30_days", label: "Last 30 days" },
  { id: "current_season", label: "Current season" },
  { id: "current_manager", label: "Current manager era" },
];

const SCENARIO_LIBRARY: Record<
  string,
  { name: string; trigger: string }
> = {
  "favourite-concedes-first": {
    name: "Favourite concedes first",
    trigger: "CONCEDED_GOAL",
  },
  "favourite-trailing-30": {
    name: "Favourite trailing after 30 minutes",
    trigger: "SCORE_STATE",
  },
  "favourite-trailing-70": {
    name: "Favourite trailing after 70 minutes",
    trigger: "SCORE_STATE",
  },
  "underdog-takes-lead": {
    name: "Underdog takes the lead",
    trigger: "GOAL",
  },
  "drawing-after-70": {
    name: "Team drawing after 70 minutes",
    trigger: "SCORE_STATE",
  },
  "leading-one-after-80": {
    name: "Leading by one after 80 minutes",
    trigger: "SCORE_STATE",
  },
  "opponent-red-while-trailing": {
    name: "Opponent red card while trailing",
    trigger: "OPPONENT_RED_CARD",
  },
  "team-red-while-leading": {
    name: "Team red card while leading",
    trigger: "RED_CARD",
  },
  "key-attacker-omitted": {
    name: "Key attacker omitted from XI",
    trigger: "TEAM_SHEET",
  },
  "early-goal-conceded": {
    name: "Early goal conceded (0–15)",
    trigger: "CONCEDED_GOAL",
  },
  "early-goal-scored": {
    name: "Early goal scored (0–15)",
    trigger: "GOAL",
  },
  "late-equaliser": {
    name: "Late equaliser conceded",
    trigger: "CONCEDED_GOAL",
  },
  "defender-booked-early": {
    name: "Defender booked before 25 minutes",
    trigger: "YELLOW_CARD",
  },
  "post-ucl-short-rest": {
    name: "Post-Champions League short rest",
    trigger: "COMPETITION_CONTEXT",
  },
};

function row(
  scenarioId: string,
  metric: Metric,
  responseWindow: ResponseWindow,
  profileWindow: ProfileWindow,
  values: Omit<
    ScenarioResponse,
    | "scenarioId"
    | "scenarioName"
    | "trigger"
    | "metric"
    | "responseWindow"
    | "profileWindow"
  >,
): ScenarioResponse {
  const scenario = SCENARIO_LIBRARY[scenarioId];
  return {
    scenarioId,
    scenarioName: scenario.name,
    trigger: scenario.trigger,
    metric,
    responseWindow,
    profileWindow,
    ...values,
  };
}

function scaleForWindow(
  base: ScenarioResponse,
  profileWindow: ProfileWindow,
): ScenarioResponse {
  if (profileWindow === "current_manager") return { ...base, profileWindow };
  if (profileWindow === "current_season") {
    const n = Math.max(6, Math.round(base.n * 0.38));
    return {
      ...base,
      profileWindow,
      n,
      confidence: Math.max(0.42, base.confidence - 0.12),
      stability: Math.max(0.38, base.stability - 0.08),
      src: Number((base.src * 0.92).toFixed(2)),
      excessResponse: Number((base.excessResponse * 0.9).toFixed(2)),
      dataQuality: n >= 18 ? "sufficient" : n >= 10 ? "limited" : "sparse",
    };
  }
  const n = Math.max(3, Math.round(base.n * 0.14));
  return {
    ...base,
    profileWindow,
    n,
    confidence: Math.max(0.28, base.confidence - 0.28),
    stability: Math.max(0.22, base.stability - 0.18),
    src: Number((base.src * 0.78).toFixed(2)),
    excessResponse: Number((base.excessResponse * 0.82).toFixed(2)),
    dataQuality: n >= 10 ? "limited" : "sparse",
  };
}

const ARSENAL_MANAGER: ScenarioResponse[] = [
  row("favourite-concedes-first", "corners", "0-15", "current_manager", {
    teamBaseline: 1.42,
    teamScenarioValue: 2.91,
    teamEffect: 1.49,
    leagueBaseline: 1.18,
    leagueScenarioValue: 1.73,
    leagueEffect: 0.55,
    excessResponse: 0.94,
    src: 2.14,
    n: 47,
    confidence: 0.86,
    stability: 0.81,
    dataQuality: "sufficient",
  }),
  row("favourite-concedes-first", "goals", "0-30", "current_manager", {
    teamBaseline: 0.41,
    teamScenarioValue: 0.78,
    teamEffect: 0.37,
    leagueBaseline: 0.38,
    leagueScenarioValue: 0.54,
    leagueEffect: 0.16,
    excessResponse: 0.21,
    src: 1.41,
    n: 47,
    confidence: 0.79,
    stability: 0.74,
    dataQuality: "sufficient",
  }),
  row("early-goal-conceded", "corners", "remainder", "current_manager", {
    teamBaseline: 6.8,
    teamScenarioValue: 9.4,
    teamEffect: 2.6,
    leagueBaseline: 5.9,
    leagueScenarioValue: 7.1,
    leagueEffect: 1.2,
    excessResponse: 1.4,
    src: 1.76,
    n: 39,
    confidence: 0.82,
    stability: 0.77,
    dataQuality: "sufficient",
  }),
  row("drawing-after-70", "corners", "remainder", "current_manager", {
    teamBaseline: 1.05,
    teamScenarioValue: 2.21,
    teamEffect: 1.16,
    leagueBaseline: 0.98,
    leagueScenarioValue: 1.41,
    leagueEffect: 0.43,
    excessResponse: 0.73,
    src: 1.62,
    n: 34,
    confidence: 0.74,
    stability: 0.69,
    dataQuality: "sufficient",
  }),
  row("opponent-red-while-trailing", "goals", "0-30", "current_manager", {
    teamBaseline: 0.52,
    teamScenarioValue: 1.18,
    teamEffect: 0.66,
    leagueBaseline: 0.49,
    leagueScenarioValue: 0.81,
    leagueEffect: 0.32,
    excessResponse: 0.34,
    src: 1.55,
    n: 18,
    confidence: 0.61,
    stability: 0.58,
    dataQuality: "limited",
  }),
  row("post-ucl-short-rest", "yellow_cards", "remainder", "current_manager", {
    teamBaseline: 1.62,
    teamScenarioValue: 2.44,
    teamEffect: 0.82,
    leagueBaseline: 1.71,
    leagueScenarioValue: 2.05,
    leagueEffect: 0.34,
    excessResponse: 0.48,
    src: 1.28,
    n: 22,
    confidence: 0.64,
    stability: 0.6,
    dataQuality: "limited",
  }),
  row("favourite-trailing-30", "corners", "0-15", "current_manager", {
    teamBaseline: 1.38,
    teamScenarioValue: 2.55,
    teamEffect: 1.17,
    leagueBaseline: 1.2,
    leagueScenarioValue: 1.68,
    leagueEffect: 0.48,
    excessResponse: 0.69,
    src: 1.48,
    n: 31,
    confidence: 0.72,
    stability: 0.71,
    dataQuality: "sufficient",
  }),
  row("early-goal-scored", "corners", "0-15", "current_manager", {
    teamBaseline: 1.42,
    teamScenarioValue: 1.21,
    teamEffect: -0.21,
    leagueBaseline: 1.18,
    leagueScenarioValue: 1.09,
    leagueEffect: -0.09,
    excessResponse: -0.12,
    src: -0.38,
    n: 44,
    confidence: 0.8,
    stability: 0.76,
    dataQuality: "sufficient",
  }),
  row("defender-booked-early", "yellow_cards", "remainder", "current_manager", {
    teamBaseline: 1.58,
    teamScenarioValue: 2.05,
    teamEffect: 0.47,
    leagueBaseline: 1.66,
    leagueScenarioValue: 2.12,
    leagueEffect: 0.46,
    excessResponse: 0.01,
    src: 0.08,
    n: 27,
    confidence: 0.58,
    stability: 0.51,
    dataQuality: "limited",
  }),
  row("leading-one-after-80", "corners", "remainder", "current_manager", {
    teamBaseline: 0.72,
    teamScenarioValue: 0.49,
    teamEffect: -0.23,
    leagueBaseline: 0.64,
    leagueScenarioValue: 0.71,
    leagueEffect: 0.07,
    excessResponse: -0.3,
    src: -0.92,
    n: 41,
    confidence: 0.77,
    stability: 0.73,
    dataQuality: "sufficient",
  }),
  row("key-attacker-omitted", "goals", "remainder", "current_manager", {
    teamBaseline: 1.91,
    teamScenarioValue: 1.42,
    teamEffect: -0.49,
    leagueBaseline: 1.55,
    leagueScenarioValue: 1.38,
    leagueEffect: -0.17,
    excessResponse: -0.32,
    src: -1.11,
    n: 16,
    confidence: 0.52,
    stability: 0.44,
    dataQuality: "limited",
  }),
  row("underdog-takes-lead", "corners", "0-15", "current_manager", {
    teamBaseline: 1.36,
    teamScenarioValue: 1.51,
    teamEffect: 0.15,
    leagueBaseline: 1.22,
    leagueScenarioValue: 1.48,
    leagueEffect: 0.26,
    excessResponse: -0.11,
    src: -0.41,
    n: 9,
    confidence: 0.34,
    stability: 0.29,
    dataQuality: "sparse",
  }),
  row("team-red-while-leading", "goals", "0-30", "current_manager", {
    teamBaseline: 0.44,
    teamScenarioValue: 0.31,
    teamEffect: -0.13,
    leagueBaseline: 0.41,
    leagueScenarioValue: 0.28,
    leagueEffect: -0.13,
    excessResponse: 0,
    src: 0.04,
    n: 11,
    confidence: 0.41,
    stability: 0.36,
    dataQuality: "sparse",
  }),
  row("late-equaliser", "corners", "remainder", "current_manager", {
    teamBaseline: 0.31,
    teamScenarioValue: 0.88,
    teamEffect: 0.57,
    leagueBaseline: 0.28,
    leagueScenarioValue: 0.54,
    leagueEffect: 0.26,
    excessResponse: 0.31,
    src: 0.94,
    n: 19,
    confidence: 0.57,
    stability: 0.49,
    dataQuality: "limited",
  }),
  row("favourite-trailing-70", "goals", "remainder", "current_manager", {
    teamBaseline: 0.22,
    teamScenarioValue: 0.61,
    teamEffect: 0.39,
    leagueBaseline: 0.19,
    leagueScenarioValue: 0.42,
    leagueEffect: 0.23,
    excessResponse: 0.16,
    src: 1.09,
    n: 24,
    confidence: 0.63,
    stability: 0.62,
    dataQuality: "limited",
  }),
];

function profilesFromManagerEra(rows: ScenarioResponse[]): Record<ProfileWindow, ScenarioResponse[]> {
  return {
    current_manager: rows,
    current_season: rows.map((item) => scaleForWindow(item, "current_season")),
    last_30_days: rows.map((item) => scaleForWindow(item, "last_30_days")),
  };
}

function lighterProfiles(
  seed: ScenarioResponse[],
  nFactor: number,
  srcFactor: number,
): Record<ProfileWindow, ScenarioResponse[]> {
  const trimmed = seed.slice(0, 8).map((item) => ({
    ...item,
    n: Math.max(4, Math.round(item.n * nFactor)),
    src: Number((item.src * srcFactor).toFixed(2)),
    excessResponse: Number((item.excessResponse * srcFactor).toFixed(2)),
    confidence: Math.max(0.3, Number((item.confidence * 0.82).toFixed(2))),
    dataQuality: (Math.round(item.n * nFactor) >= 18
      ? "sufficient"
      : Math.round(item.n * nFactor) >= 10
        ? "limited"
        : "sparse") as DataQuality,
  }));
  return profilesFromManagerEra(trimmed);
}

const TEAMS: TeamIdentity[] = [
  { id: "arsenal", name: "Arsenal", shortName: "ARS", competitionId: "premier-league", city: "London", colours: "Red / white", srcIndex: 81, primaryMetric: "corners", dataQuality: "sufficient", sampleSize: 47 },
  { id: "chelsea", name: "Chelsea", shortName: "CHE", competitionId: "premier-league", city: "London", colours: "Blue", srcIndex: 64, primaryMetric: "corners", dataQuality: "sufficient", sampleSize: 29 },
  { id: "liverpool", name: "Liverpool", shortName: "LIV", competitionId: "premier-league", city: "Liverpool", colours: "Red", srcIndex: 73, primaryMetric: "goals", dataQuality: "sufficient", sampleSize: 36 },
  { id: "manchester-city", name: "Manchester City", shortName: "MCI", competitionId: "premier-league", city: "Manchester", colours: "Sky blue", srcIndex: 69, primaryMetric: "goals", dataQuality: "sufficient", sampleSize: 33 },
  { id: "brighton", name: "Brighton & Hove Albion", shortName: "BHA", competitionId: "premier-league", city: "Brighton", colours: "Blue / white", srcIndex: 71, primaryMetric: "corners", dataQuality: "sufficient", sampleSize: 28 },
  { id: "newcastle", name: "Newcastle United", shortName: "NEW", competitionId: "premier-league", city: "Newcastle", colours: "Black / white", srcIndex: 58, primaryMetric: "corners", dataQuality: "limited", sampleSize: 21 },
  { id: "tottenham", name: "Tottenham Hotspur", shortName: "TOT", competitionId: "premier-league", city: "London", colours: "White", srcIndex: 55, primaryMetric: "corners", dataQuality: "limited", sampleSize: 24 },
  { id: "fulham", name: "Fulham", shortName: "FUL", competitionId: "premier-league", city: "London", colours: "White / black", srcIndex: 46, primaryMetric: "corners", dataQuality: "limited", sampleSize: 18 },
  { id: "manchester-united", name: "Manchester United", shortName: "MUN", competitionId: "premier-league", city: "Manchester", colours: "Red", srcIndex: 49, primaryMetric: "yellow_cards", dataQuality: "limited", sampleSize: 18 },
  { id: "leeds", name: "Leeds United", shortName: "LEE", competitionId: "championship", city: "Leeds", colours: "White", srcIndex: 62, primaryMetric: "corners", dataQuality: "limited", sampleSize: 19 },
  { id: "leicester", name: "Leicester City", shortName: "LEI", competitionId: "championship", city: "Leicester", colours: "Blue", srcIndex: 57, primaryMetric: "goals", dataQuality: "limited", sampleSize: 16 },
  { id: "southampton", name: "Southampton", shortName: "SOU", competitionId: "championship", city: "Southampton", colours: "Red / white", srcIndex: 44, primaryMetric: "corners", dataQuality: "limited", sampleSize: 14 },
  { id: "burnley", name: "Burnley", shortName: "BUR", competitionId: "championship", city: "Burnley", colours: "Claret", srcIndex: 41, primaryMetric: "yellow_cards", dataQuality: "sparse", sampleSize: 11 },
  { id: "real-madrid", name: "Real Madrid", shortName: "RMA", competitionId: "la-liga", city: "Madrid", colours: "White", srcIndex: 76, primaryMetric: "goals", dataQuality: "sufficient", sampleSize: 31 },
  { id: "barcelona", name: "Barcelona", shortName: "BAR", competitionId: "la-liga", city: "Barcelona", colours: "Blugrana", srcIndex: 72, primaryMetric: "corners", dataQuality: "sufficient", sampleSize: 27 },
  { id: "atletico-madrid", name: "Atlético Madrid", shortName: "ATM", competitionId: "la-liga", city: "Madrid", colours: "Red / white", srcIndex: 66, primaryMetric: "yellow_cards", dataQuality: "sufficient", sampleSize: 25 },
  { id: "valencia", name: "Valencia", shortName: "VAL", competitionId: "la-liga", city: "Valencia", colours: "White / orange", srcIndex: 47, primaryMetric: "corners", dataQuality: "limited", sampleSize: 15 },
  { id: "sevilla", name: "Sevilla", shortName: "SEV", competitionId: "la-liga", city: "Seville", colours: "White / red", srcIndex: 52, primaryMetric: "corners", dataQuality: "limited", sampleSize: 17 },
];

const DASHBOARDS: Record<string, TeamDashboard> = {
  arsenal: {
    team: TEAMS[0],
    nextFixture: {
      opponentId: "fulham",
      opponentName: "Fulham",
      competitionId: "premier-league",
      venue: "H",
      kickoffIso: "2026-09-12T11:30:00.000Z",
      kickoffLabel: "Sat 12 Sep · 12:30 BST",
      context: "Featured walkthrough fixture · Arsenal v Fulham · pre-match favourite · 60 minutes to kickoff (11:30 local).",
      impliedFavourite: "home",
    },
    currentManager: {
      managerId: "arteta",
      managerName: "Mikel Arteta",
      appointmentType: "PERMANENT",
      phase: "ESTABLISHED",
      regimeStart: "2019-12-22",
      matchesInRegime: 286,
      isCurrent: true,
    },
    previousManager: {
      managerId: "emery",
      managerName: "Unai Emery",
      appointmentType: "PERMANENT",
      phase: "ESTABLISHED",
      regimeStart: "2018-05-23",
      matchesInRegime: 78,
      isCurrent: false,
    },
    profiles: profilesFromManagerEra(ARSENAL_MANAGER),
    regimeComparison: [
      {
        scenarioId: "favourite-concedes-first",
        scenarioName: "Favourite concedes first",
        metric: "corners",
        responseWindow: "0-15",
        currentSrc: 2.14,
        previousSrc: 0.71,
        currentN: 47,
        previousN: 21,
        currentManager: "Arteta",
        previousManager: "Emery",
      },
      {
        scenarioId: "early-goal-conceded",
        scenarioName: "Early goal conceded (0–15)",
        metric: "corners",
        responseWindow: "remainder",
        currentSrc: 1.76,
        previousSrc: 0.88,
        currentN: 39,
        previousN: 18,
        currentManager: "Arteta",
        previousManager: "Emery",
      },
      {
        scenarioId: "drawing-after-70",
        scenarioName: "Team drawing after 70 minutes",
        metric: "corners",
        responseWindow: "remainder",
        currentSrc: 1.62,
        previousSrc: 0.44,
        currentN: 34,
        previousN: 16,
        currentManager: "Arteta",
        previousManager: "Emery",
      },
      {
        scenarioId: "leading-one-after-80",
        scenarioName: "Leading by one after 80 minutes",
        metric: "corners",
        responseWindow: "remainder",
        currentSrc: -0.92,
        previousSrc: -0.21,
        currentN: 41,
        previousN: 19,
        currentManager: "Arteta",
        previousManager: "Emery",
      },
    ],
    notes: [
      "Arsenal is the richest DEMO profile in this slice: full SRC table, manager-era split, and next-kickoff context.",
      "SRC is an excess-response summary versus the league benchmark, not a raw correlation.",
      "Champions League short-rest is included as a competition-context scenario; it is still fixture data.",
    ],
  },
};

function pack(
  team: TeamIdentity,
  next: NextFixture,
  current: ManagerRegime,
  previous: ManagerRegime | null,
  nFactor: number,
  srcFactor: number,
  notes: string[],
): TeamDashboard {
  return {
    team,
    nextFixture: next,
    currentManager: current,
    previousManager: previous,
    profiles: lighterProfiles(ARSENAL_MANAGER, nFactor, srcFactor),
    regimeComparison: previous
      ? [
          {
            scenarioId: "favourite-concedes-first",
            scenarioName: "Favourite concedes first",
            metric: "corners",
            responseWindow: "0-15",
            currentSrc: Number((2.14 * srcFactor).toFixed(2)),
            previousSrc: Number((0.9 * srcFactor).toFixed(2)),
            currentN: Math.max(6, Math.round(47 * nFactor)),
            previousN: Math.max(4, Math.round(18 * nFactor)),
            currentManager: current.managerName.split(" ").slice(-1)[0],
            previousManager: previous.managerName.split(" ").slice(-1)[0],
          },
        ]
      : [],
    notes,
  };
}

function fillDashboards() {
  const byId = Object.fromEntries(TEAMS.map((team) => [team.id, team]));

  DASHBOARDS.chelsea = pack(
    byId.chelsea,
    {
      opponentId: "brighton",
      opponentName: "Brighton & Hove Albion",
      competitionId: "premier-league",
      venue: "A",
      kickoffIso: "2026-09-13T14:00:00.000Z",
      kickoffLabel: "Sun 13 Sep · 15:00 BST",
      context: "Away · mid-table implied probability bucket.",
      impliedFavourite: "away",
    },
    { managerId: "maresca", managerName: "Enzo Maresca", appointmentType: "PERMANENT", phase: "DEVELOPING", regimeStart: "2024-07-01", matchesInRegime: 74, isCurrent: true },
    { managerId: "pochettino", managerName: "Mauricio Pochettino", appointmentType: "PERMANENT", phase: "DEVELOPING", regimeStart: "2023-07-01", matchesInRegime: 51, isCurrent: false },
    0.62,
    0.71,
    ["Chelsea fixture set is thinner than Arsenal and is labelled DEMO throughout."],
  );

  DASHBOARDS.liverpool = pack(
    byId.liverpool,
    {
      opponentId: "newcastle",
      opponentName: "Newcastle United",
      competitionId: "premier-league",
      venue: "H",
      kickoffIso: "2026-09-13T16:30:00.000Z",
      kickoffLabel: "Sun 13 Sep · 17:30 BST",
      context: "Home favourite · Sunday evening slot.",
      impliedFavourite: "home",
    },
    { managerId: "slot", managerName: "Arne Slot", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2024-06-01", matchesInRegime: 92, isCurrent: true },
    { managerId: "klopp", managerName: "Jürgen Klopp", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2015-10-08", matchesInRegime: 491, isCurrent: false },
    0.74,
    0.88,
    ["Liverpool includes a previous-regime comparison row for UI review only."],
  );

  DASHBOARDS["manchester-city"] = pack(
    byId["manchester-city"],
    {
      opponentId: "arsenal",
      opponentName: "Arsenal",
      competitionId: "premier-league",
      venue: "A",
      kickoffIso: "2026-09-19T16:30:00.000Z",
      kickoffLabel: "Sat 19 Sep · 17:30 BST",
      context: "Away to Arsenal · Champions League midweek in the week prior (short-rest flag).",
      impliedFavourite: "even",
    },
    { managerId: "guardiola", managerName: "Pep Guardiola", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2016-07-01", matchesInRegime: 512, isCurrent: true },
    null,
    0.7,
    0.8,
    ["No previous-regime split in this DEMO set — current manager era is the only comparable sample."],
  );

  DASHBOARDS.brighton = pack(
    byId.brighton,
    {
      opponentId: "chelsea",
      opponentName: "Chelsea",
      competitionId: "premier-league",
      venue: "H",
      kickoffIso: "2026-09-13T14:00:00.000Z",
      kickoffLabel: "Sun 13 Sep · 15:00 BST",
      context: "Home underdog versus Chelsea.",
      impliedFavourite: "away",
    },
    { managerId: "hurzeler", managerName: "Fabian Hürzeler", appointmentType: "PERMANENT", phase: "DEVELOPING", regimeStart: "2024-06-15", matchesInRegime: 68, isCurrent: true },
    { managerId: "dezerbi", managerName: "Roberto De Zerbi", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2022-09-18", matchesInRegime: 72, isCurrent: false },
    0.58,
    0.93,
    ["Brighton is a high-SRC DEMO club on corners despite a smaller sample."],
  );

  DASHBOARDS.newcastle = pack(
    byId.newcastle,
    {
      opponentId: "liverpool",
      opponentName: "Liverpool",
      competitionId: "premier-league",
      venue: "A",
      kickoffIso: "2026-09-13T16:30:00.000Z",
      kickoffLabel: "Sun 13 Sep · 17:30 BST",
      context: "Away underdog.",
      impliedFavourite: "away",
    },
    { managerId: "howe", managerName: "Eddie Howe", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2021-11-08", matchesInRegime: 198, isCurrent: true },
    null,
    0.45,
    0.62,
    ["Newcastle DEMO sample is limited — shrink toward league benchmark in a live model."],
  );

  DASHBOARDS.tottenham = pack(
    byId.tottenham,
    {
      opponentId: "newcastle",
      opponentName: "Newcastle United",
      competitionId: "premier-league",
      venue: "H",
      kickoffIso: "2026-09-13T13:00:00.000Z",
      kickoffLabel: "Sun 13 Sep · 14:00 BST",
      context: "Home · not the Arsenal 12:30 featured fixture.",
      impliedFavourite: "home",
    },
    { managerId: "frank", managerName: "Thomas Frank", appointmentType: "PERMANENT", phase: "EARLY", regimeStart: "2025-06-12", matchesInRegime: 8, isCurrent: true },
    { managerId: "postecoglou", managerName: "Ange Postecoglou", appointmentType: "PERMANENT", phase: "DEVELOPING", regimeStart: "2023-07-01", matchesInRegime: 80, isCurrent: false },
    0.5,
    0.55,
    ["Current manager phase is EARLY — last-30-days and manager-era samples should not be blended in a live model."],
  );

  DASHBOARDS["manchester-united"] = pack(
    byId["manchester-united"],
    {
      opponentId: "leicester",
      opponentName: "Leicester City",
      competitionId: "premier-league",
      venue: "H",
      kickoffIso: "2026-09-14T19:00:00.000Z",
      kickoffLabel: "Mon 14 Sep · 20:00 BST",
      context: "Home favourite · Monday night.",
      impliedFavourite: "home",
    },
    { managerId: "amorim", managerName: "Rúben Amorim", appointmentType: "PERMANENT", phase: "DEVELOPING", regimeStart: "2024-11-11", matchesInRegime: 62, isCurrent: true },
    { managerId: "tenhag", managerName: "Erik ten Hag", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2022-05-23", matchesInRegime: 128, isCurrent: false },
    0.4,
    0.48,
    ["United DEMO profile is weaker / noisier by design."],
  );

  DASHBOARDS.fulham = pack(
    byId.fulham,
    {
      opponentId: "arsenal",
      opponentName: "Arsenal",
      competitionId: "premier-league",
      venue: "A",
      kickoffIso: "2026-09-12T11:30:00.000Z",
      kickoffLabel: "Sat 12 Sep · 12:30 BST",
      context: "Away counterpart to the featured Arsenal v Fulham 12:30 walkthrough.",
      impliedFavourite: "away",
    },
    { managerId: "silva", managerName: "Marco Silva", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2021-07-01", matchesInRegime: 164, isCurrent: true },
    null,
    0.38,
    0.42,
    ["Fulham is the featured away side. Same kickoff as the Arsenal dashboard."],
  );

  DASHBOARDS.leeds = pack(
    byId.leeds,
    {
      opponentId: "leicester",
      opponentName: "Leicester City",
      competitionId: "championship",
      venue: "H",
      kickoffIso: "2026-09-12T14:00:00.000Z",
      kickoffLabel: "Sat 12 Sep · 15:00 BST",
      context: "Championship promotion pace · home favourite.",
      impliedFavourite: "home",
    },
    { managerId: "farke", managerName: "Daniel Farke", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2023-07-04", matchesInRegime: 112, isCurrent: true },
    null,
    0.42,
    0.77,
    ["Championship coverage is representative, not complete."],
  );

  DASHBOARDS.leicester = pack(
    byId.leicester,
    {
      opponentId: "leeds",
      opponentName: "Leeds United",
      competitionId: "championship",
      venue: "A",
      kickoffIso: "2026-09-12T14:00:00.000Z",
      kickoffLabel: "Sat 12 Sep · 15:00 BST",
      context: "Away · Championship.",
      impliedFavourite: "away",
    },
    { managerId: "van-nistelrooy", managerName: "Ruud van Nistelrooy", appointmentType: "PERMANENT", phase: "DEVELOPING", regimeStart: "2024-12-01", matchesInRegime: 48, isCurrent: true },
    null,
    0.36,
    0.66,
    ["Championship DEMO profile — limited N."],
  );

  DASHBOARDS.southampton = pack(
    byId.southampton,
    {
      opponentId: "burnley",
      opponentName: "Burnley",
      competitionId: "championship",
      venue: "A",
      kickoffIso: "2026-09-13T11:30:00.000Z",
      kickoffLabel: "Sun 13 Sep · 12:30 BST",
      context: "Away · Championship lunchtime.",
      impliedFavourite: "even",
    },
    { managerId: "still", managerName: "Will Still", appointmentType: "PERMANENT", phase: "EARLY", regimeStart: "2025-12-01", matchesInRegime: 9, isCurrent: true },
    null,
    0.3,
    0.41,
    ["Early manager phase — DEMO coefficients are shrunk and sparse."],
  );

  DASHBOARDS.burnley = pack(
    byId.burnley,
    {
      opponentId: "southampton",
      opponentName: "Southampton",
      competitionId: "championship",
      venue: "H",
      kickoffIso: "2026-09-13T11:30:00.000Z",
      kickoffLabel: "Sun 13 Sep · 12:30 BST",
      context: "Home · Championship.",
      impliedFavourite: "home",
    },
    { managerId: "parker", managerName: "Scott Parker", appointmentType: "PERMANENT", phase: "DEVELOPING", regimeStart: "2024-07-05", matchesInRegime: 58, isCurrent: true },
    null,
    0.24,
    0.36,
    ["Sparse DEMO coverage. Do not treat SRC as reliable."],
  );

  DASHBOARDS["real-madrid"] = pack(
    byId["real-madrid"],
    {
      opponentId: "barcelona",
      opponentName: "Barcelona",
      competitionId: "la-liga",
      venue: "H",
      kickoffIso: "2026-09-12T19:00:00.000Z",
      kickoffLabel: "Sat 12 Sep · 21:00 CEST",
      context: "El Clásico · La Liga · UCL midweek in the same window.",
      impliedFavourite: "home",
    },
    { managerId: "alonso", managerName: "Xabi Alonso", appointmentType: "PERMANENT", phase: "DEVELOPING", regimeStart: "2025-06-01", matchesInRegime: 14, isCurrent: true },
    { managerId: "ancelotti", managerName: "Carlo Ancelotti", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2021-06-01", matchesInRegime: 233, isCurrent: false },
    0.66,
    0.84,
    ["La Liga DEMO coverage. Champions League short-rest is represented on the profile."],
  );

  DASHBOARDS.barcelona = pack(
    byId.barcelona,
    {
      opponentId: "real-madrid",
      opponentName: "Real Madrid",
      competitionId: "la-liga",
      venue: "A",
      kickoffIso: "2026-09-12T19:00:00.000Z",
      kickoffLabel: "Sat 12 Sep · 21:00 CEST",
      context: "Away Clásico.",
      impliedFavourite: "home",
    },
    { managerId: "flick", managerName: "Hansi Flick", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2024-07-01", matchesInRegime: 88, isCurrent: true },
    null,
    0.58,
    0.79,
    ["La Liga DEMO profile."],
  );

  DASHBOARDS["atletico-madrid"] = pack(
    byId["atletico-madrid"],
    {
      opponentId: "valencia",
      opponentName: "Valencia",
      competitionId: "la-liga",
      venue: "H",
      kickoffIso: "2026-09-13T16:15:00.000Z",
      kickoffLabel: "Sun 13 Sep · 18:15 CEST",
      context: "Home favourite · card-heavy DEMO identity.",
      impliedFavourite: "home",
    },
    { managerId: "simeone", managerName: "Diego Simeone", appointmentType: "PERMANENT", phase: "ESTABLISHED", regimeStart: "2011-12-23", matchesInRegime: 680, isCurrent: true },
    null,
    0.54,
    0.7,
    ["Long-running manager era in DEMO data — ESTABLISHED phase."],
  );

  DASHBOARDS.valencia = pack(
    byId.valencia,
    {
      opponentId: "atletico-madrid",
      opponentName: "Atlético Madrid",
      competitionId: "la-liga",
      venue: "A",
      kickoffIso: "2026-09-13T16:15:00.000Z",
      kickoffLabel: "Sun 13 Sep · 18:15 CEST",
      context: "Away underdog.",
      impliedFavourite: "away",
    },
    { managerId: "corberan", managerName: "Carlos Corberán", appointmentType: "PERMANENT", phase: "DEVELOPING", regimeStart: "2024-12-23", matchesInRegime: 41, isCurrent: true },
    null,
    0.32,
    0.44,
    ["Limited La Liga DEMO sample."],
  );

  DASHBOARDS.sevilla = pack(
    byId.sevilla,
    {
      opponentId: "barcelona",
      opponentName: "Barcelona",
      competitionId: "la-liga",
      venue: "H",
      kickoffIso: "2026-09-20T16:15:00.000Z",
      kickoffLabel: "Sun 20 Sep · 18:15 CEST",
      context: "Home · La Liga.",
      impliedFavourite: "away",
    },
    { managerId: "garcia", managerName: "García Pimienta", appointmentType: "PERMANENT", phase: "DEVELOPING", regimeStart: "2024-06-01", matchesInRegime: 55, isCurrent: true },
    null,
    0.35,
    0.52,
    ["La Liga DEMO profile with limited N."],
  );
}

fillDashboards();

export function listCompetitions(): Competition[] {
  return COMPETITIONS;
}

export function listTeams(): TeamIdentity[] {
  return TEAMS;
}

export function getCompetition(id: CompetitionId): Competition {
  return COMPETITIONS.find((item) => item.id === id)!;
}

export function getTeam(teamId: string): TeamIdentity | undefined {
  return TEAMS.find((team) => team.id === teamId);
}

export function getTeamDashboard(teamId: string): TeamDashboard | undefined {
  return DASHBOARDS[teamId];
}

export function strongestResponses(rows: ScenarioResponse[], count = 4): ScenarioResponse[] {
  return [...rows].sort((a, b) => b.src - a.src).slice(0, count);
}

export function weakestResponses(rows: ScenarioResponse[], count = 3): ScenarioResponse[] {
  return [...rows].sort((a, b) => a.src - b.src).slice(0, count);
}

export function scenarioLabHref(params: {
  teamId: string;
  scenarioId: string;
  metric: Metric;
  responseWindow: ResponseWindow;
  profileWindow: ProfileWindow;
}): string {
  const search = new URLSearchParams({
    team: params.teamId,
    scenario: params.scenarioId,
    metric: params.metric,
    window: params.responseWindow,
    period: params.profileWindow,
    source: "team-explorer-demo",
    data: "demo",
  });
  return `/scenario-lab?${search.toString()}`;
}

export function formatSigned(value: number, digits = 2): string {
  const formatted = value.toFixed(digits);
  return value > 0 ? `+${formatted}` : formatted;
}

export function formatPct(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function metricLabel(metric: Metric): string {
  switch (metric) {
    case "corners":
      return "Corners";
    case "yellow_cards":
      return "Yellow cards";
    case "red_cards":
      return "Red cards";
    case "goals":
      return "Goals";
  }
}

export function qualityLabel(quality: DataQuality): string {
  switch (quality) {
    case "sufficient":
      return "Sufficient";
    case "limited":
      return "Limited";
    case "sparse":
      return "Sparse";
  }
}

export function phaseLabel(phase: ManagerPhase): string {
  switch (phase) {
    case "DEBUT":
      return "Debut";
    case "VERY_EARLY":
      return "Very early (2–3)";
    case "EARLY":
      return "Early (4–10)";
    case "DEVELOPING":
      return "Developing (11–25)";
    case "ESTABLISHED":
      return "Established (26+)";
  }
}
