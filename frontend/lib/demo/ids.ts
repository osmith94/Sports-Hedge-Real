/**
 * Shared Sports Hedge demo identity contract.
 *
 * Research and Arbitrage UI surfaces must use these IDs so Arsenal's next
 * fixture, scenario encodings and team keys stay consistent.
 */

export type CompetitionId = "premier-league" | "championship" | "la-liga" | "champions-league";
export type TeamId = string;
export type FixtureId = string;
export type ScenarioId = string;
export type ResponseWindow = "0-5" | "0-10" | "0-15" | "0-30" | "remainder";
export type MetricId = "corners" | "yellow_cards" | "red_cards" | "goals" | "match_result";

export const FEATURED_FIXTURE_ID = "pl-2026-09-12-arsenal-fulham" as const;
export const FEATURED_HOME_TEAM_ID = "arsenal";
export const FEATURED_AWAY_TEAM_ID = "fulham";
export const FEATURED_KICKOFF_ISO = "2026-09-12T11:30:00.000Z";
export const FEATURED_KICKOFF_LABEL = "Sat 12 Sep · 12:30 BST";

const TEAM_ALIASES: Record<string, string> = {
  "man-city": "manchester-city",
  mci: "manchester-city",
  "man-united": "manchester-united",
  mun: "manchester-united",
};

const SCENARIO_ALIASES: Record<string, string> = {
  favourite_concedes_first: "favourite-concedes-first",
  "fav-trails-underdog": "favourite-trails-underdog",
  favourite_trails_underdog: "favourite-trails-underdog",
  favourite_trailing_after_60: "favourite-trailing-60",
  favourite_trailing_after_70: "favourite-trailing-70",
  "favourite-trailing-70": "favourite-trailing-70",
  "trailing-after-70": "favourite-trailing-70",
  favourite_trailing_after_30: "favourite-trailing-30",
  "favourite-trailing-30": "favourite-trailing-30",
  underdog_takes_the_lead: "underdog-takes-lead",
  "underdog-takes-lead": "underdog-takes-lead",
  drawing_after_70: "drawing-after-70",
  leading_by_one_after_70: "leading-one-after-70",
  leading_by_one_after_80: "leading-one-after-80",
  opponent_red_while_trailing: "opponent-red-while-trailing",
  "opponent-red-trailing": "opponent-red-while-trailing",
  team_red_while_leading: "team-red-while-leading",
  early_goal_scored: "early-goal-scored",
  early_goal_conceded: "early-goal-conceded",
  late_equaliser: "late-equaliser",
  defender_booked_early: "defender-booked-early",
  post_ucl_short_rest: "post-ucl-short-rest",
  short_rest_post_cl: "post-ucl-short-rest",
  new_manager_first_10: "new-manager-first-10",
  derby_favourite_concedes_first: "favourite-concedes-first",
};

const WINDOW_ALIASES: Record<string, ResponseWindow> = {
  "5m": "0-5",
  "0_5": "0-5",
  "0-5": "0-5",
  "10m": "0-10",
  "0_10": "0-10",
  "0-10": "0-10",
  "15m": "0-15",
  "0_15": "0-15",
  "0-15": "0-15",
  "30m": "0-30",
  "0_30": "0-30",
  "0-30": "0-30",
  rest: "remainder",
  remainder: "remainder",
};

const COMPETITION_ALIASES: Record<string, CompetitionId> = {
  premier_league: "premier-league",
  "premier-league": "premier-league",
  championship: "championship",
  la_liga: "la-liga",
  "la-liga": "la-liga",
  champions_league: "champions-league",
  "champions-league": "champions-league",
};

export function canonicalTeamId(raw: string | null | undefined): string {
  if (!raw) return "";
  return TEAM_ALIASES[raw] ?? raw;
}

export function canonicalScenarioId(raw: string | null | undefined): string {
  if (!raw) return "";
  return SCENARIO_ALIASES[raw] ?? raw;
}

export function canonicalResponseWindow(raw: string | null | undefined): ResponseWindow | undefined {
  if (!raw) return undefined;
  return WINDOW_ALIASES[raw];
}

export function canonicalCompetitionId(raw: string | null | undefined): CompetitionId | undefined {
  if (!raw) return undefined;
  return COMPETITION_ALIASES[raw];
}

/** Scenario Lab still uses a compact window encoding in its local ranking engine. */
export function scenarioLabWindow(
  window: ResponseWindow | undefined,
): "5m" | "10m" | "15m" | "30m" | "rest" | undefined {
  if (!window) return undefined;
  switch (window) {
    case "0-5":
      return "5m";
    case "0-10":
      return "10m";
    case "0-15":
      return "15m";
    case "0-30":
      return "30m";
    case "remainder":
      return "rest";
  }
}

export function plannerWindow(
  window: ResponseWindow | undefined,
): "0_5" | "0_10" | "0_15" | "0_30" | "remainder" | undefined {
  if (!window) return undefined;
  switch (window) {
    case "0-5":
      return "0_5";
    case "0-10":
      return "0_10";
    case "0-15":
      return "0_15";
    case "0-30":
      return "0_30";
    case "remainder":
      return "remainder";
  }
}

export function scenarioLabScenarioId(canonical: string): string {
  switch (canonical) {
    case "favourite-trails-underdog":
      return "fav-trails-underdog";
    case "favourite-trailing-70":
      return "trailing-after-70";
    case "opponent-red-while-trailing":
      return "opponent-red-trailing";
    default:
      return canonical;
  }
}

export function scenarioLabHref(args: {
  teamId: string;
  scenarioId: string;
  metric: string;
  window: string;
  source?: string;
}): string {
  const params = new URLSearchParams({
    team: canonicalTeamId(args.teamId),
    scenario: canonicalScenarioId(args.scenarioId),
    metric: args.metric,
    source: args.source ?? "demo",
    data: "demo",
  });
  const window = canonicalResponseWindow(args.window);
  if (window) params.set("window", window);
  return `/scenario-lab?${params.toString()}`;
}

export function teamHref(teamId: string): string {
  return `/teams/${canonicalTeamId(teamId)}`;
}

export function matchdayHref(): string {
  return "/matchday";
}
