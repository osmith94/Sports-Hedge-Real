/**
 * Typed DEMO / FIXTURE DATA for the Matchday research hub.
 * These values are illustrative only. They are not live odds, live
 * scenario coefficients, or current trading opportunities.
 */

export type DataQuality = "sufficient" | "thin" | "illustrative";
export type ConfidenceBand = "high" | "medium" | "low";
export type SrcCallout = "high" | "low" | "neutral";
export type FavouriteRole = "favourite" | "underdog" | "even";
export type ManagerPhase = "DEBUT" | "VERY_EARLY" | "EARLY" | "DEVELOPING" | "ESTABLISHED";
export type SeasonPhase = "OPENING_3" | "OPENING_5" | "OPENING_10" | "MID_SEASON" | "RUN_IN";
export type AppointmentType = "PERMANENT" | "INTERIM" | "CARETAKER";

export type MatchdayTeamContext = {
  teamId: string;
  name: string;
  shortName: string;
  role: FavouriteRole;
  demoImpliedProbability: number;
  managerName: string;
  appointmentType: AppointmentType;
  managerPhase: ManagerPhase;
  managerMatchNumber: number;
  seasonPhase: SeasonPhase;
  seasonMatchNumber: number;
  regimeChangeRecent: boolean;
  regimeNote: string;
};

export type ScenarioProfile = {
  scenarioId: string;
  title: string;
  trigger: string;
  metric: string;
  window: string;
  teamId: string;
  src: number;
  teamExcessResponse: string;
  sampleSize: number;
  confidence: ConfidenceBand;
  dataQuality: DataQuality;
  callout: SrcCallout;
  stability: string;
  note: string;
};

export type MatchdayFixture = {
  fixtureId: string;
  competition: string;
  venue: string;
  kickoffIso: string;
  kickoffLabel: string;
  kickoffLocal: string;
  featured: boolean;
  home: MatchdayTeamContext;
  away: MatchdayTeamContext;
  preMatchNote: string;
  scenarios: ScenarioProfile[];
};

export const MATCHDAY_DATA_DISCLAIMER =
  "DEMO / FIXTURE DATA — illustrative research context only. Not live prices, not a current opportunity, and not a betting recommendation.";

export const MATCHDAY_HUB = {
  dateLabel: "Saturday 12 September 2026",
  dayLabel: "Matchday",
  localTimeContext: "Research window: ~11:30 before a 12:30 kickoff",
  paperMode: "PAPER MODE",
  dataLabel: "DEMO / FIXTURE DATA",
} as const;

export const matchdayFixtures: MatchdayFixture[] = [
  {
    fixtureId: "pl-2026-09-12-arsenal-fulham",
    competition: "Premier League",
    venue: "Emirates Stadium",
    kickoffIso: "2026-09-12T11:30:00.000Z",
    kickoffLabel: "12:30 BST",
    kickoffLocal: "Sat 12 Sep · 12:30",
    featured: true,
    home: {
      teamId: "arsenal",
      name: "Arsenal",
      shortName: "ARS",
      role: "favourite",
      demoImpliedProbability: 0.62,
      managerName: "Mikel Arteta",
      appointmentType: "PERMANENT",
      managerPhase: "ESTABLISHED",
      managerMatchNumber: 312,
      seasonPhase: "OPENING_5",
      seasonMatchNumber: 4,
      regimeChangeRecent: false,
      regimeNote: "Long-running Arteta sample. Treat as one established regime unless a later structural break is tagged.",
    },
    away: {
      teamId: "fulham",
      name: "Fulham",
      shortName: "FUL",
      role: "underdog",
      demoImpliedProbability: 0.18,
      managerName: "Marco Silva",
      appointmentType: "PERMANENT",
      managerPhase: "ESTABLISHED",
      managerMatchNumber: 164,
      seasonPhase: "OPENING_5",
      seasonMatchNumber: 4,
      regimeChangeRecent: false,
      regimeNote: "Established Silva tenure. Opponent-strength and underdog-state filters still apply.",
    },
    preMatchNote:
      "Demo favourite/underdog split for research browsing only. Implied probabilities are fixture placeholders, not a live book.",
    scenarios: [
      {
        scenarioId: "favourite-concedes-first",
        title: "Favourite concedes first",
        trigger: "CONCEDED_GOAL",
        metric: "corners",
        window: "0-15",
        teamId: "arsenal",
        src: 1.42,
        teamExcessResponse: "+0.86 corners vs league effect",
        sampleSize: 41,
        confidence: "high",
        dataQuality: "sufficient",
        callout: "high",
        stability: "stable across last 3 seasons in-era",
        note: "Demo SRC: Arsenal’s corner response after falling behind as favourite is stronger than the Premier League benchmark.",
      },
      {
        scenarioId: "favourite-trailing-60",
        title: "Favourite trailing after 60 minutes",
        trigger: "SCORE_STATE",
        metric: "goals",
        window: "remainder",
        teamId: "arsenal",
        src: 0.74,
        teamExcessResponse: "+0.11 goals vs league effect",
        sampleSize: 27,
        confidence: "medium",
        dataQuality: "sufficient",
        callout: "neutral",
        stability: "moderate · shrink toward league on thin late-era splits",
        note: "Inspect in Scenario Lab rather than treating the coefficient as a pricing signal.",
      },
      {
        scenarioId: "leading-one-after-70",
        title: "Leading by one after 70 minutes",
        trigger: "SCORE_STATE",
        metric: "yellow_cards",
        window: "0-15",
        teamId: "arsenal",
        src: -0.38,
        teamExcessResponse: "−0.09 cards vs league effect",
        sampleSize: 33,
        confidence: "medium",
        dataQuality: "sufficient",
        callout: "low",
        stability: "stable but small effect",
        note: "Low-SRC callout: card response is weaker than the league when protecting a one-goal lead.",
      },
      {
        scenarioId: "early-goal-scored",
        title: "Early goal scored",
        trigger: "GOAL",
        metric: "corners",
        window: "0-10",
        teamId: "arsenal",
        src: 0.91,
        teamExcessResponse: "+0.41 corners vs league effect",
        sampleSize: 36,
        confidence: "high",
        dataQuality: "sufficient",
        callout: "high",
        stability: "high",
        note: "Use as a ranking candidate in Scenario Lab, not as evidence of an executable market.",
      },
      {
        scenarioId: "underdog-takes-lead",
        title: "Underdog takes the lead",
        trigger: "GOAL",
        metric: "corners",
        window: "0-15",
        teamId: "fulham",
        src: 0.22,
        teamExcessResponse: "+0.08 corners vs league effect",
        sampleSize: 18,
        confidence: "low",
        dataQuality: "thin",
        callout: "neutral",
        stability: "thin sample · pooled toward league",
        note: "Fulham underdog-lead sample is thin. Flagged as low confidence.",
      },
    ],
  },
  {
    fixtureId: "pl-2026-09-12-newcastle-brighton",
    competition: "Premier League",
    venue: "St James’ Park",
    kickoffIso: "2026-09-12T14:00:00.000Z",
    kickoffLabel: "15:00 BST",
    kickoffLocal: "Sat 12 Sep · 15:00",
    featured: false,
    home: {
      teamId: "newcastle",
      name: "Newcastle United",
      shortName: "NEW",
      role: "favourite",
      demoImpliedProbability: 0.51,
      managerName: "Eddie Howe",
      appointmentType: "PERMANENT",
      managerPhase: "ESTABLISHED",
      managerMatchNumber: 198,
      seasonPhase: "OPENING_5",
      seasonMatchNumber: 4,
      regimeChangeRecent: false,
      regimeNote: "Established Howe era. Home favourite state is the primary filter for this card.",
    },
    away: {
      teamId: "brighton",
      name: "Brighton & Hove Albion",
      shortName: "BHA",
      role: "underdog",
      demoImpliedProbability: 0.26,
      managerName: "Fabian Hürzeler",
      appointmentType: "PERMANENT",
      managerPhase: "DEVELOPING",
      managerMatchNumber: 52,
      seasonPhase: "OPENING_5",
      seasonMatchNumber: 4,
      regimeChangeRecent: false,
      regimeNote: "Developing managerial sample. Prefer era-split views over club-lifetime averages.",
    },
    preMatchNote:
      "Secondary demo fixture. Scenario list is a shortlist for inspection, not a claim that any market is currently mispriced.",
    scenarios: [
      {
        scenarioId: "favourite-concedes-first",
        title: "Favourite concedes first",
        trigger: "CONCEDED_GOAL",
        metric: "corners",
        window: "0-15",
        teamId: "newcastle",
        src: 0.55,
        teamExcessResponse: "+0.21 corners vs league effect",
        sampleSize: 29,
        confidence: "medium",
        dataQuality: "sufficient",
        callout: "neutral",
        stability: "moderate",
        note: "Compare with the Arsenal profile in Scenario Lab rather than stacking both as the same favourite-behind pattern.",
      },
      {
        scenarioId: "new-manager-first-10",
        title: "Developing-era response vs league",
        trigger: "REGIME_CONTEXT",
        metric: "corners",
        window: "0-15",
        teamId: "brighton",
        src: 0.11,
        teamExcessResponse: "near league baseline",
        sampleSize: 12,
        confidence: "low",
        dataQuality: "thin",
        callout: "low",
        stability: "too early to treat as stable",
        note: "Low-SRC / thin-N callout. Do not blend with previous-manager Brighton history.",
      },
    ],
  },
  {
    fixtureId: "pl-2026-09-12-liverpool-everton",
    competition: "Premier League",
    venue: "Anfield",
    kickoffIso: "2026-09-12T16:30:00.000Z",
    kickoffLabel: "17:30 BST",
    kickoffLocal: "Sat 12 Sep · 17:30",
    featured: false,
    home: {
      teamId: "liverpool",
      name: "Liverpool",
      shortName: "LIV",
      role: "favourite",
      demoImpliedProbability: 0.58,
      managerName: "Arne Slot",
      appointmentType: "PERMANENT",
      managerPhase: "DEVELOPING",
      managerMatchNumber: 84,
      seasonPhase: "OPENING_5",
      seasonMatchNumber: 4,
      regimeChangeRecent: false,
      regimeNote: "Developing Slot sample. Prefer current-manager SRPs over earlier club history.",
    },
    away: {
      teamId: "everton",
      name: "Everton",
      shortName: "EVE",
      role: "underdog",
      demoImpliedProbability: 0.21,
      managerName: "David Moyes",
      appointmentType: "PERMANENT",
      managerPhase: "EARLY",
      managerMatchNumber: 9,
      seasonPhase: "OPENING_5",
      seasonMatchNumber: 4,
      regimeChangeRecent: true,
      regimeNote: "Recent regime marker: early Moyes tenure. Club-lifetime Everton profiles should not be used silently.",
    },
    preMatchNote:
      "Included to show a recent-regime underdog next to an established favourite. Demo values only.",
    scenarios: [
      {
        scenarioId: "favourite-concedes-first",
        title: "Favourite concedes first",
        trigger: "CONCEDED_GOAL",
        metric: "corners",
        window: "0-15",
        teamId: "liverpool",
        src: 0.88,
        teamExcessResponse: "+0.34 corners vs league effect",
        sampleSize: 22,
        confidence: "medium",
        dataQuality: "sufficient",
        callout: "high",
        stability: "era-split recommended",
        note: "High-SRC candidate inside the current manager sample. Still a research ranking, not a trade.",
      },
      {
        scenarioId: "new-manager-first-10",
        title: "New-manager first 10",
        trigger: "NEW_MANAGER_FIRST_10",
        metric: "yellow_cards",
        window: "remainder",
        teamId: "everton",
        src: -0.19,
        teamExcessResponse: "near league · wide interval",
        sampleSize: 8,
        confidence: "low",
        dataQuality: "thin",
        callout: "low",
        stability: "insufficient",
        note: "Low-SRC and thin-N. Regime-aware analysis should shrink hard toward the league.",
      },
    ],
  },
];

export function scenarioLabHref(profile: Pick<ScenarioProfile, "teamId" | "scenarioId" | "metric" | "window">): string {
  const params = new URLSearchParams({
    team: profile.teamId,
    scenario: profile.scenarioId,
    metric: profile.metric,
    window: profile.window,
    source: "matchday",
    data: "demo",
  });
  return `/scenario-lab?${params.toString()}`;
}

export function teamHref(teamId: string): string {
  return `/teams/${teamId}`;
}

export function formatImpliedProbability(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function srcLabel(src: number): string {
  const sign = src > 0 ? "+" : "";
  return `${sign}${src.toFixed(2)}`;
}
