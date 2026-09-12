import { FEATURED_FIXTURE_ID, canonicalScenarioId } from "./ids";
import { bestNetQuote, evPerPound, feeAssumption, valueState, withEffectiveEconomics } from "./economics";
import type { EffectiveQuote, ValueState } from "./types";

function quote(args: {
  venue: EffectiveQuote["venue"];
  headline: number;
  retrievedAtLabel: string;
  quoteAgeMinutes: number;
  role: "reference" | "available";
  rate: number | null;
  known: boolean;
  basis?: EffectiveQuote["fee"]["feeBasis"];
  settlementEquivalent?: boolean;
  liquidityNote?: string;
}): EffectiveQuote {
  return withEffectiveEconomics({
    venue: args.venue,
    side: "BACK",
    headlineDecimal: args.headline,
    fee: feeAssumption({
      venue: args.venue,
      side: "BACK",
      feeBasis: args.known ? args.basis ?? "PROFIT_COMMISSION" : "UNKNOWN",
      rate: args.known ? args.rate : null,
      known: args.known,
      capturedAtLabel: args.retrievedAtLabel,
      provenance: "demo-fee-assumption",
    }),
    retrievedAtLabel: args.retrievedAtLabel,
    quoteAgeMinutes: args.quoteAgeMinutes,
    role: args.role,
    settlementEquivalent: args.settlementEquivalent ?? true,
    liquidityNote: args.liquidityNote ?? "DEMO depth placeholder",
  });
}

export type DemoValueSignal = {
  signalId: string;
  fixtureId: string;
  teamId: string;
  teamName: string;
  fixtureLabel: string;
  scenarioId: string;
  scenarioTitle: string;
  metric: string;
  marketFamily: string;
  line: string;
  responseWindow: "0-5" | "0-10" | "0-15" | "0-30" | "remainder";
  src: number;
  sampleSize: number;
  confidence: "high" | "medium" | "low";
  stability: number;
  dataQuality: "sufficient" | "limited" | "sparse";
  modelProbability: number;
  quotes: EffectiveQuote[];
  quoteFreshness: string;
  polymarketOmittedReason?: string;
  rankingNote: string;
  valueState: ValueState;
};

function enrich(signal: Omit<DemoValueSignal, "valueState">): DemoValueSignal {
  return {
    ...signal,
    valueState: valueState({
      quotes: signal.quotes,
      modelProbability: signal.modelProbability,
      sampleSize: signal.sampleSize,
      confidence: signal.confidence,
    }),
  };
}

export const demoValueSignals: DemoValueSignal[] = [
  enrich({
    signalId: "ars-fav-trails-corners-15",
    fixtureId: FEATURED_FIXTURE_ID,
    teamId: "arsenal",
    teamName: "Arsenal",
    fixtureLabel: "Arsenal v Fulham",
    scenarioId: "favourite-concedes-first",
    scenarioTitle: "Favourite concedes first → corners next 15m",
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
      quote({
        venue: "Matchbook",
        headline: 2.18,
        retrievedAtLabel: "11:26 BST",
        quoteAgeMinutes: 4,
        role: "reference",
        rate: 0.02,
        known: true,
        liquidityNote: "DEMO · £1,200 visible",
      }),
      quote({
        venue: "Smarkets",
        headline: 2.28,
        retrievedAtLabel: "11:27 BST",
        quoteAgeMinutes: 3,
        role: "available",
        rate: 0.12,
        known: true,
        liquidityNote: "DEMO · higher headline, worse net after 12% profit commission",
      }),
    ],
    quoteFreshness: "Best net quote 4m stale · snapshot 11:30 BST",
    polymarketOmittedReason:
      "Polymarket omitted: no economically equivalent in-play team-corners contract with matching settlement.",
    rankingNote:
      "Ranked on net/effective odds. Smarkets 2.28 headline loses to Matchbook 2.18 after the demo fee snapshots.",
  }),
  enrich({
    signalId: "ars-fav-trails-match-result-no-value",
    fixtureId: FEATURED_FIXTURE_ID,
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
      quote({
        venue: "Matchbook",
        headline: 1.72,
        retrievedAtLabel: "11:24 BST",
        quoteAgeMinutes: 6,
        role: "reference",
        rate: 0.02,
        known: true,
      }),
      quote({
        venue: "Smarkets",
        headline: 1.7,
        retrievedAtLabel: "11:25 BST",
        quoteAgeMinutes: 5,
        role: "available",
        rate: 0.02,
        known: true,
      }),
    ],
    quoteFreshness: "Best net quote 6m stale · snapshot 11:30 BST",
    rankingNote: "High SRC but the market already prices the response. Demonstrates NO_VALUE.",
  }),
  enrich({
    signalId: "ars-missing-costs",
    fixtureId: FEATURED_FIXTURE_ID,
    teamId: "arsenal",
    teamName: "Arsenal",
    fixtureLabel: "Arsenal v Fulham",
    scenarioId: "early-goal-scored",
    scenarioTitle: "Early goal scored → corners next 10m",
    metric: "corners",
    marketFamily: "Team corners",
    line: "Over 0.5 (next 10m, Arsenal)",
    responseWindow: "0-10",
    src: 0.91,
    sampleSize: 36,
    confidence: "high",
    stability: 0.76,
    dataQuality: "sufficient",
    modelProbability: 0.51,
    quotes: [
      quote({
        venue: "Matchbook",
        headline: 2.05,
        retrievedAtLabel: "11:20 BST",
        quoteAgeMinutes: 10,
        role: "reference",
        rate: null,
        known: false,
      }),
    ],
    quoteFreshness: "Fee snapshot UNKNOWN · fail closed",
    rankingNote: "Unknown costs produce MISSING_COSTS. They are not treated as 0%.",
  }),
  enrich({
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
      quote({ venue: "Matchbook", headline: 2.72, retrievedAtLabel: "11:22 BST", quoteAgeMinutes: 8, role: "reference", rate: 0.02, known: true }),
      quote({ venue: "Smarkets", headline: 2.66, retrievedAtLabel: "11:21 BST", quoteAgeMinutes: 9, role: "available", rate: 0.02, known: true }),
    ],
    quoteFreshness: "Best quote 8m stale · snapshot 11:30 BST",
    polymarketOmittedReason: "Polymarket omitted: card totals are not settlement-equivalent.",
    rankingNote: "Odds-weighted rank is below Arsenal corners despite a usable SRC.",
  }),
  enrich({
    signalId: "ars-stale-quote-demo",
    fixtureId: FEATURED_FIXTURE_ID,
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
    quotes: [
      quote({
        venue: "Matchbook",
        headline: 2.2,
        retrievedAtLabel: "11:08 BST",
        quoteAgeMinutes: 22,
        role: "reference",
        rate: 0.02,
        known: true,
      }),
    ],
    quoteFreshness: "Best net quote 22m stale · DEMO max age 10m · cannot rank VALUE",
    rankingNote:
      "Would clear the EV gate on net odds, but quote age 22m exceeds the DEMO ASSUMPTION of 10 minutes, so the state is STALE_QUOTE.",
  }),
];

export function rankedDemoValueSignals(signals: DemoValueSignal[] = demoValueSignals): DemoValueSignal[] {
  return [...signals].sort((a, b) => {
    const rank = (signal: DemoValueSignal) => {
      if (signal.valueState !== "VALUE") return Number.NEGATIVE_INFINITY;
      const best = bestNetQuote(signal.quotes);
      if (!best || best.netDecimal === null) return Number.NEGATIVE_INFINITY;
      return evPerPound(signal.modelProbability, best.netDecimal);
    };
    return rank(b) - rank(a);
  });
}

export function valueForScenario(args: { teamId: string; scenarioId: string }): DemoValueSignal | undefined {
  const scenarioId = canonicalScenarioId(args.scenarioId);
  const exact = demoValueSignals.find(
    (signal) => signal.teamId === args.teamId && signal.scenarioId === scenarioId,
  );
  if (exact) return exact;
  if (scenarioId === "favourite-trails-underdog" || scenarioId === "fav-trails-underdog") {
    return demoValueSignals.find(
      (signal) => signal.teamId === args.teamId && signal.scenarioId === "favourite-concedes-first",
    );
  }
  return undefined;
}
