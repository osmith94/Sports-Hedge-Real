import { DEMO_MAX_QUOTE_AGE_MINUTES } from "./demo/economics";
import type { ValueState } from "./demo/types";

/**
 * Typed DEMO / FIXTURE DATA for Research Home.
 *
 * Illustrative stand-in for future valuation APIs. Numbers are invented for
 * UI review. They are not live odds, live SRC observations, or executable
 * opportunities. Rankings require a model probability and a current quote;
 * SRC alone is never used as the sort key.
 */

export const RESEARCH_DEMO_BANNER = "DEMO / FIXTURE DATA";

export const RESEARCH_DEMO_DISCLAIMER =
  "DEMO / FIXTURE DATA — illustrative research snapshot for UI review. These are not live prices, not live Scenario Response Profiles, and not trading opportunities. Value rankings are odds-weighted analysis, not arbitrage and not guaranteed profit.";

export type CompetitionId =
  | "premier-league"
  | "championship"
  | "la-liga"
  | "champions-league";

export type FavouriteRole = "favourite" | "underdog" | "even";
export type ManagerPhase = "DEBUT" | "VERY_EARLY" | "EARLY" | "DEVELOPING" | "ESTABLISHED";
export type AppointmentType = "PERMANENT" | "INTERIM" | "CARETAKER";
export type DataQuality = "sufficient" | "limited" | "sparse";
export type ConfidenceBand = "high" | "medium" | "low";
export type QuoteVenue = "Matchbook" | "Smarkets" | "Polymarket";
export type MetricFamily = "corners" | "yellow_cards" | "red_cards" | "goals" | "match_result";
export type ResponseWindow = "0-5" | "0-10" | "0-15" | "0-30" | "remainder";

export type VenueQuote = {
  venue: QuoteVenue;
  decimalPrice: number;
  impliedProbability: number;
  retrievedAtLabel: string;
  quoteAgeMinutes: number;
  role: "reference" | "available";
  settlementEquivalent: true;
  feeBasis: "PROFIT_COMMISSION" | "UNKNOWN";
  feeRate: number | null;
  feeKnown: boolean;
  netDecimal: number | null;
};

export type ResearchTeam = {
  teamId: string;
  name: string;
  shortName: string;
};

export type ResearchFixture = {
  fixtureId: string;
  competition: string;
  competitionId: CompetitionId;
  venue: string;
  kickoffIso: string;
  kickoffLabel: string;
  kickoffLocal: string;
  featured: boolean;
  home: ResearchTeam;
  away: ResearchTeam;
  favouriteTeamId: string;
  underdogTeamId: string;
  favouriteRoleNote: string;
  homeImpliedProbability: number;
  awayImpliedProbability: number;
  homeManager: string;
  awayManager: string;
  homeManagerPhase: ManagerPhase;
  awayManagerPhase: ManagerPhase;
  homeAppointmentType: AppointmentType;
  awayAppointmentType: AppointmentType;
  regimeStatus: string;
  relevantScenarioCount: number;
  strongestSignalId: string;
  matchdayHref: string;
};

export type ValueSignal = {
  signalId: string;
  fixtureId: string;
  teamId: string;
  teamName: string;
  fixtureLabel: string;
  scenarioId: string;
  scenarioTitle: string;
  metric: MetricFamily;
  marketFamily: string;
  line: string;
  responseWindow: ResponseWindow;
  src: number;
  sampleSize: number;
  confidence: ConfidenceBand;
  stability: number;
  dataQuality: DataQuality;
  modelProbability: number;
  quotes: VenueQuote[];
  quoteFreshness: string;
  polymarketOmittedReason?: string;
  rankingNote: string;
};

export const RESEARCH_HUB = {
  snapshotLabel: "Saturday 12 September 2026 · 11:30 BST",
  kickoffContext: "Arsenal v Fulham kicks off in 60 minutes",
  paperMode: "PAPER MODE",
  dataLabel: RESEARCH_DEMO_BANNER,
  rankingRule: "Ranked by estimated EV per £1 at the best net/effective equivalent quote after demo fee snapshots, not by SRC or headline odds. Quotes older than the DEMO ASSUMPTION of 10 minutes cannot rank VALUE.",
} as const;

export function teamHref(teamId: string): string {
  return `/teams/${teamId}`;
}

export function scenarioLabHref(args: {
  teamId: string;
  scenarioId: string;
  metric: string;
  window: string;
}): string {
  const params = new URLSearchParams({
    team: args.teamId,
    scenario: args.scenarioId,
    metric: args.metric,
    window: args.window,
    source: "research-home",
    data: "demo",
  });
  return `/scenario-lab?${params.toString()}`;
}

export function impliedFromDecimal(decimalPrice: number): number {
  return 1 / decimalPrice;
}

export function probabilityEdgePp(modelProbability: number, marketProbability: number): number {
  return (modelProbability - marketProbability) * 100;
}

export function evPerPound(modelProbability: number, decimalPrice: number): number {
  return modelProbability * decimalPrice - 1;
}

export function referenceQuote(signal: ValueSignal): VenueQuote | undefined {
  return signal.quotes.find((quote) => quote.role === "reference") ?? signal.quotes[0];
}

export function netFromHeadline(decimalPrice: number, feeKnown: boolean, feeRate: number | null, feeBasis: VenueQuote["feeBasis"]): number | null {
  if (!feeKnown || feeBasis === "UNKNOWN" || feeRate === null) return null;
  return 1 + (decimalPrice - 1) * (1 - feeRate);
}

export function bestAvailableQuote(signal: ValueSignal): VenueQuote | undefined {
  const usable = signal.quotes.filter((quote) => quote.netDecimal !== null);
  if (usable.length === 0) return undefined;
  return [...usable].sort((a, b) => (b.netDecimal ?? 0) - (a.netDecimal ?? 0))[0];
}

export function rankedValueSignals(signals: ValueSignal[]): ValueSignal[] {
  return [...signals].sort((a, b) => {
    const rank = (signal: ValueSignal) => {
      if (valueStateFor(signal) !== "VALUE") return Number.NEGATIVE_INFINITY;
      const best = bestAvailableQuote(signal);
      if (!best || best.netDecimal === null) return Number.NEGATIVE_INFINITY;
      return evPerPound(signal.modelProbability, best.netDecimal);
    };
    return rank(b) - rank(a);
  });
}

export function marketProbability(signal: ValueSignal): number | null {
  const quote = bestAvailableQuote(signal);
  if (!quote || quote.netDecimal === null) return null;
  return 1 / quote.netDecimal;
}

export function valueStateFor(signal: ValueSignal): ValueState {
  const best = bestAvailableQuote(signal);
  if (!best || best.netDecimal === null) return "MISSING_COSTS";
  if (best.quoteAgeMinutes > DEMO_MAX_QUOTE_AGE_MINUTES) return "STALE_QUOTE";
  const ev = evPerPound(signal.modelProbability, best.netDecimal);
  return ev >= 0.02 ? "VALUE" : "NO_VALUE";
}

export function formatPercent(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatPp(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}pp`;
}

export function formatEv(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(3)}`;
}

export function formatSrc(src: number): string {
  const sign = src > 0 ? "+" : "";
  return `${sign}${src.toFixed(2)}`;
}

export function formatDecimal(price: number): string {
  return price.toFixed(2);
}

function quote(
  venue: QuoteVenue,
  decimalPrice: number,
  retrievedAtLabel: string,
  quoteAgeMinutes: number,
  role: VenueQuote["role"],
  feeRate = 0.02,
  feeKnown = true,
): VenueQuote {
  const net = netFromHeadline(decimalPrice, feeKnown, feeRate, feeKnown ? "PROFIT_COMMISSION" : "UNKNOWN");
  return {
    venue,
    decimalPrice,
    impliedProbability: Number(impliedFromDecimal(decimalPrice).toFixed(4)),
    retrievedAtLabel,
    quoteAgeMinutes,
    role,
    settlementEquivalent: true,
    feeBasis: feeKnown ? "PROFIT_COMMISSION" : "UNKNOWN",
    feeRate: feeKnown ? feeRate : null,
    feeKnown,
    netDecimal: net,
  };
}

export const researchFixtures: ResearchFixture[] = [
  {
    fixtureId: "pl-2026-09-12-arsenal-fulham",
    competition: "Premier League",
    competitionId: "premier-league",
    venue: "Emirates Stadium",
    kickoffIso: "2026-09-12T11:30:00.000Z",
    kickoffLabel: "12:30 BST",
    kickoffLocal: "Sat 12 Sep · 12:30",
    featured: true,
    home: { teamId: "arsenal", name: "Arsenal", shortName: "ARS" },
    away: { teamId: "fulham", name: "Fulham", shortName: "FUL" },
    favouriteTeamId: "arsenal",
    underdogTeamId: "fulham",
    favouriteRoleNote: "Arsenal favourite · Fulham underdog",
    homeImpliedProbability: 0.62,
    awayImpliedProbability: 0.18,
    homeManager: "Mikel Arteta",
    awayManager: "Marco Silva",
    homeManagerPhase: "ESTABLISHED",
    awayManagerPhase: "ESTABLISHED",
    homeAppointmentType: "PERMANENT",
    awayAppointmentType: "PERMANENT",
    regimeStatus: "Arteta established · Silva established · no recent regime break",
    relevantScenarioCount: 5,
    strongestSignalId: "ars-fav-trails-corners-15",
    matchdayHref: "/matchday",
  },
  {
    fixtureId: "pl-2026-09-12-newcastle-brighton",
    competition: "Premier League",
    competitionId: "premier-league",
    venue: "St James’ Park",
    kickoffIso: "2026-09-12T14:00:00.000Z",
    kickoffLabel: "15:00 BST",
    kickoffLocal: "Sat 12 Sep · 15:00",
    featured: false,
    home: { teamId: "newcastle", name: "Newcastle United", shortName: "NEW" },
    away: { teamId: "brighton", name: "Brighton & Hove Albion", shortName: "BHA" },
    favouriteTeamId: "newcastle",
    underdogTeamId: "brighton",
    favouriteRoleNote: "Newcastle favourite · Brighton underdog",
    homeImpliedProbability: 0.51,
    awayImpliedProbability: 0.26,
    homeManager: "Eddie Howe",
    awayManager: "Fabian Hürzeler",
    homeManagerPhase: "ESTABLISHED",
    awayManagerPhase: "DEVELOPING",
    homeAppointmentType: "PERMANENT",
    awayAppointmentType: "PERMANENT",
    regimeStatus: "Howe established · Hürzeler developing — do not blend Brighton eras",
    relevantScenarioCount: 2,
    strongestSignalId: "new-lead-late-opp-cards",
    matchdayHref: "/matchday",
  },
  {
    fixtureId: "pl-2026-09-12-liverpool-everton",
    competition: "Premier League",
    competitionId: "premier-league",
    venue: "Anfield",
    kickoffIso: "2026-09-12T16:30:00.000Z",
    kickoffLabel: "17:30 BST",
    kickoffLocal: "Sat 12 Sep · 17:30",
    featured: false,
    home: { teamId: "liverpool", name: "Liverpool", shortName: "LIV" },
    away: { teamId: "everton", name: "Everton", shortName: "EVE" },
    favouriteTeamId: "liverpool",
    underdogTeamId: "everton",
    favouriteRoleNote: "Liverpool favourite · Everton underdog",
    homeImpliedProbability: 0.58,
    awayImpliedProbability: 0.21,
    homeManager: "Arne Slot",
    awayManager: "David Moyes",
    homeManagerPhase: "DEVELOPING",
    awayManagerPhase: "EARLY",
    homeAppointmentType: "PERMANENT",
    awayAppointmentType: "PERMANENT",
    regimeStatus: "Slot developing · Moyes early tenure (recent regime marker)",
    relevantScenarioCount: 2,
    strongestSignalId: "liv-red-card-reversion",
    matchdayHref: "/matchday",
  },
  {
    fixtureId: "ch-2026-09-12-leeds-leicester",
    competition: "Championship",
    competitionId: "championship",
    venue: "Elland Road",
    kickoffIso: "2026-09-12T14:00:00.000Z",
    kickoffLabel: "15:00 BST",
    kickoffLocal: "Sat 12 Sep · 15:00",
    featured: false,
    home: { teamId: "leeds", name: "Leeds United", shortName: "LEE" },
    away: { teamId: "leicester", name: "Leicester City", shortName: "LEI" },
    favouriteTeamId: "leeds",
    underdogTeamId: "leicester",
    favouriteRoleNote: "Leeds favourite · Leicester underdog",
    homeImpliedProbability: 0.48,
    awayImpliedProbability: 0.28,
    homeManager: "Daniel Farke",
    awayManager: "Martí Cifuentes",
    homeManagerPhase: "ESTABLISHED",
    awayManagerPhase: "EARLY",
    homeAppointmentType: "PERMANENT",
    awayAppointmentType: "PERMANENT",
    regimeStatus: "Championship sample · shrink harder toward league on thin N",
    relevantScenarioCount: 3,
    strongestSignalId: "lee-fav-trails-corners-15",
    matchdayHref: "/matchday",
  },
  {
    fixtureId: "ll-2026-09-13-real-sociedad",
    competition: "La Liga",
    competitionId: "la-liga",
    venue: "Santiago Bernabéu",
    kickoffIso: "2026-09-13T19:00:00.000Z",
    kickoffLabel: "21:00 CEST",
    kickoffLocal: "Sun 13 Sep · 21:00",
    featured: false,
    home: { teamId: "real-madrid", name: "Real Madrid", shortName: "RMA" },
    away: { teamId: "real-sociedad", name: "Real Sociedad", shortName: "RSO" },
    favouriteTeamId: "real-madrid",
    underdogTeamId: "real-sociedad",
    favouriteRoleNote: "Real Madrid favourite · Real Sociedad underdog",
    homeImpliedProbability: 0.71,
    awayImpliedProbability: 0.12,
    homeManager: "Xabi Alonso",
    awayManager: "Imanol Alguacil",
    homeManagerPhase: "DEVELOPING",
    awayManagerPhase: "ESTABLISHED",
    homeAppointmentType: "PERMANENT",
    awayAppointmentType: "PERMANENT",
    regimeStatus: "Current-manager Madrid sample still developing",
    relevantScenarioCount: 4,
    strongestSignalId: "rma-lead-late-opp-cards",
    matchdayHref: "/matchday",
  },
  {
    fixtureId: "ucl-2026-09-16-arsenal-inter",
    competition: "Champions League",
    competitionId: "champions-league",
    venue: "Emirates Stadium",
    kickoffIso: "2026-09-16T19:00:00.000Z",
    kickoffLabel: "20:00 BST",
    kickoffLocal: "Tue 16 Sep · 20:00",
    featured: false,
    home: { teamId: "arsenal", name: "Arsenal", shortName: "ARS" },
    away: { teamId: "inter", name: "Inter", shortName: "INT" },
    favouriteTeamId: "arsenal",
    underdogTeamId: "inter",
    favouriteRoleNote: "Arsenal favourite · Inter underdog (demo)",
    homeImpliedProbability: 0.46,
    awayImpliedProbability: 0.29,
    homeManager: "Mikel Arteta",
    awayManager: "Simone Inzaghi",
    homeManagerPhase: "ESTABLISHED",
    awayManagerPhase: "ESTABLISHED",
    homeAppointmentType: "PERMANENT",
    awayAppointmentType: "PERMANENT",
    regimeStatus: "UCL-ready card · post-weekend short-rest context for midweek",
    relevantScenarioCount: 3,
    strongestSignalId: "ars-post-ucl-rest-placeholder",
    matchdayHref: "/matchday",
  },
];

export const researchValueSignals: ValueSignal[] = [
  {
    signalId: "ars-fav-trails-corners-15",
    fixtureId: "pl-2026-09-12-arsenal-fulham",
    teamId: "arsenal",
    teamName: "Arsenal",
    fixtureLabel: "Arsenal v Fulham",
    scenarioId: "favourite-concedes-first",
    scenarioTitle: "Favourite trails underdog → corners next 15m",
    metric: "corners",
    marketFamily: "Team corners",
    line: "Over 1.5 (next 15m, Arsenal)",
    responseWindow: "0-15",
    src: 1.42,
    sampleSize: 41,
    confidence: "high",
    stability: 0.81,
    dataQuality: "sufficient",
    modelProbability: 0.542,
    quotes: [
      quote("Matchbook", 2.18, "11:26 BST", 4, "reference", 0.02),
      quote("Smarkets", 2.28, "11:27 BST", 3, "available", 0.12),
    ],
    quoteFreshness: "Best quote 4m stale · snapshot 11:30 BST",
    polymarketOmittedReason:
      "Polymarket omitted: no economically equivalent in-play team-corners contract with matching settlement.",
    rankingNote:
      "Highest demo EV on net/effective odds. Smarkets 2.28 headline loses to Matchbook 2.18 after the 12% vs 2% demo fee snapshots. SRC is supporting context, not the rank key.",
  },
  {
    signalId: "ars-stale-quote-demo",
    fixtureId: "pl-2026-09-12-arsenal-fulham",
    teamId: "arsenal",
    teamName: "Arsenal",
    fixtureLabel: "Arsenal v Fulham",
    scenarioId: "early-goal-conceded",
    scenarioTitle: "Early goal conceded → corners next 15m (stale quote)",
    metric: "corners",
    marketFamily: "Team corners",
    line: "Over 1.5 (next 15m, Arsenal)",
    responseWindow: "0-15",
    src: 1.38,
    sampleSize: 38,
    confidence: "high",
    stability: 0.79,
    dataQuality: "sufficient",
    modelProbability: 0.54,
    quotes: [quote("Matchbook", 2.2, "11:08 BST", 22, "reference")],
    quoteFreshness: "Best net quote 22m stale · DEMO ASSUMPTION max age 10m · cannot rank VALUE",
    rankingNote:
      "Would clear the EV gate on net odds, but quote age 22m exceeds the DEMO ASSUMPTION of 10 minutes, so the state is STALE_QUOTE.",
  },
  {
    signalId: "ars-fav-trails-match-result-no-value",
    fixtureId: "pl-2026-09-12-arsenal-fulham",
    teamId: "arsenal",
    teamName: "Arsenal",
    fixtureLabel: "Arsenal v Fulham",
    scenarioId: "favourite-concedes-first",
    scenarioTitle: "Favourite concedes first → match result remainder",
    metric: "goals",
    marketFamily: "Match result (Arsenal)",
    line: "Arsenal (remainder after conceding first)",
    responseWindow: "remainder",
    src: 1.41,
    sampleSize: 47,
    confidence: "high",
    stability: 0.74,
    dataQuality: "sufficient",
    modelProbability: 0.49,
    quotes: [
      quote("Matchbook", 1.72, "11:24 BST", 6, "reference", 0.02),
      quote("Smarkets", 1.7, "11:25 BST", 5, "available", 0.02),
    ],
    quoteFreshness: "Best net quote 6m stale · snapshot 11:30 BST",
    rankingNote: "High SRC but the market already prices the response. Demonstrates NO_VALUE.",
  },
  {
    signalId: "new-lead-late-opp-cards",
    fixtureId: "pl-2026-09-12-newcastle-brighton",
    teamId: "newcastle",
    teamName: "Newcastle United",
    fixtureLabel: "Newcastle United v Brighton",
    scenarioId: "leading-one-after-70",
    scenarioTitle: "Team leading late → opponent cards",
    metric: "yellow_cards",
    marketFamily: "Opponent yellow cards",
    line: "Over 0.5 (next 15m, Brighton)",
    responseWindow: "0-15",
    src: 0.66,
    sampleSize: 29,
    confidence: "medium",
    stability: 0.64,
    dataQuality: "sufficient",
    modelProbability: 0.418,
    quotes: [
      quote("Matchbook", 2.72, "11:22 BST", 8, "reference"),
      quote("Smarkets", 2.66, "11:21 BST", 9, "available"),
    ],
    quoteFreshness: "Best quote 8m stale · snapshot 11:30 BST",
    polymarketOmittedReason:
      "Polymarket omitted: card totals are not settlement-equivalent to the listed exchange markets.",
    rankingNote: "Odds-weighted rank is lower than Arsenal corners despite a usable SRC.",
  },
  {
    signalId: "liv-red-card-reversion",
    fixtureId: "pl-2026-09-12-liverpool-everton",
    teamId: "liverpool",
    teamName: "Liverpool",
    fixtureLabel: "Liverpool v Everton",
    scenarioId: "team-red-while-leading",
    scenarioTitle: "Red-card response / probability reversion",
    metric: "goals",
    marketFamily: "Match result (Liverpool)",
    line: "Liverpool (in-play, after red while leading)",
    responseWindow: "remainder",
    src: 0.88,
    sampleSize: 22,
    confidence: "medium",
    stability: 0.58,
    dataQuality: "sufficient",
    modelProbability: 0.445,
    quotes: [
      quote("Matchbook", 2.08, "11:18 BST", 12, "reference"),
      quote("Smarkets", 2.12, "11:19 BST", 11, "available"),
      quote("Polymarket", 2.05, "11:15 BST", 15, "available"),
    ],
    quoteFreshness: "Best quote 11m stale · DEMO max age 10m · STALE_QUOTE",
    rankingNote:
      "Polymarket included only because the demo contract is treated as economically equivalent to exchange match-winner settlement. Still analysis, not a locked arb.",
  },
  {
    signalId: "lee-fav-trails-corners-15",
    fixtureId: "ch-2026-09-12-leeds-leicester",
    teamId: "leeds",
    teamName: "Leeds United",
    fixtureLabel: "Leeds United v Leicester City",
    scenarioId: "favourite-concedes-first",
    scenarioTitle: "Favourite trails underdog → corners next 15m",
    metric: "corners",
    marketFamily: "Team corners",
    line: "Over 1.5 (next 15m, Leeds)",
    responseWindow: "0-15",
    src: 0.71,
    sampleSize: 18,
    confidence: "low",
    stability: 0.47,
    dataQuality: "limited",
    modelProbability: 0.39,
    quotes: [
      quote("Matchbook", 2.42, "11:20 BST", 10, "reference"),
      quote("Smarkets", 2.38, "11:20 BST", 10, "available"),
    ],
    quoteFreshness: "Best quote 10m stale · snapshot 11:30 BST",
    polymarketOmittedReason:
      "Polymarket omitted: Championship team-corners settlement is not equivalent on the demo prediction venue.",
    rankingNote: "Lower N and limited data quality reduce usable value even when SRC is positive.",
  },
  {
    signalId: "rma-lead-late-opp-cards",
    fixtureId: "ll-2026-09-13-real-sociedad",
    teamId: "real-madrid",
    teamName: "Real Madrid",
    fixtureLabel: "Real Madrid v Real Sociedad",
    scenarioId: "leading-one-after-80",
    scenarioTitle: "Team leading late → opponent cards",
    metric: "yellow_cards",
    marketFamily: "Opponent yellow cards",
    line: "Over 0.5 (remainder, Real Sociedad)",
    responseWindow: "remainder",
    src: 0.41,
    sampleSize: 24,
    confidence: "medium",
    stability: 0.61,
    dataQuality: "sufficient",
    modelProbability: 0.352,
    quotes: [
      quote("Matchbook", 2.96, "11:12 BST", 18, "reference"),
      quote("Smarkets", 3.05, "11:14 BST", 16, "available"),
    ],
    quoteFreshness: "Best quote 16m stale · DEMO max age 10m · STALE_QUOTE",
    polymarketOmittedReason:
      "Polymarket omitted: La Liga card markets are not mapped as settlement-equivalent in this demo.",
    rankingNote: "Model probability is close to the market; estimated EV is small after the best quote.",
  },
  {
    signalId: "ars-post-ucl-rest-placeholder",
    fixtureId: "ucl-2026-09-16-arsenal-inter",
    teamId: "arsenal",
    teamName: "Arsenal",
    fixtureLabel: "Arsenal v Inter",
    scenarioId: "post-ucl-short-rest",
    scenarioTitle: "Post-weekend / short-rest corner response",
    metric: "corners",
    marketFamily: "Team corners",
    line: "Over 5.5 (full match, Arsenal)",
    responseWindow: "remainder",
    src: 0.54,
    sampleSize: 14,
    confidence: "low",
    stability: 0.39,
    dataQuality: "sparse",
    modelProbability: 0.48,
    quotes: [quote("Matchbook", 1.92, "11:05 BST", 25, "reference")],
    quoteFreshness: "Single-venue quote 25m stale · DEMO max age 10m · STALE_QUOTE",
    polymarketOmittedReason:
      "Polymarket omitted: full-match team-corners is not economically equivalent to listed prediction contracts.",
    rankingNote:
      "SRC is mid-pack but EV is weak because the Matchbook implied probability already sits near the model. Demonstrates that SRC ≠ value.",
  },
];

export const rankedResearchSignals = rankedValueSignals(researchValueSignals);

export const researchBrowse = [
  {
    href: "/teams",
    label: "Teams",
    icon: "TM",
    description: "Club directory and Scenario Response Profile dashboards.",
  },
  {
    href: "/matchday",
    label: "Fixtures / Matchday",
    icon: "MD",
    description: "Kickoff board, regime context and per-fixture scenario shortlists.",
  },
  {
    href: "/scenario-lab",
    label: "Scenario Lab",
    icon: "SL",
    description: "Team × scenario matrix, SRC, sample size and era splits.",
  },
  {
    href: "/scenario-planner",
    label: "Scenario Planner",
    icon: "PL",
    description: "Paper watch rules with SRC and odds/value gates.",
  },
  {
    href: "/trends",
    label: "Trends",
    icon: "TR",
    description: "Historical cohort behaviour across markets and events.",
  },
  {
    href: "/market-intelligence",
    label: "Market Intelligence",
    icon: "MI",
    description: "Event-driven price, spread and liquidity movement.",
  },
] as const;

export const arbitrageWorkspace = {
  href: "/",
  title: "Arbitrage Workspace",
  status: "Separate product area · paper scans only",
  note: "Cross-venue discrepancies after matching, fees and depth. Scenario value signals on this page are not arb legs and do not imply locked profit.",
} as const;

export function fixtureById(fixtureId: string): ResearchFixture | undefined {
  return researchFixtures.find((fixture) => fixture.fixtureId === fixtureId);
}

export function signalById(signalId: string): ValueSignal | undefined {
  return researchValueSignals.find((signal) => signal.signalId === signalId);
}

export function strongestSignalForFixture(fixture: ResearchFixture): ValueSignal | undefined {
  return signalById(fixture.strongestSignalId);
}
