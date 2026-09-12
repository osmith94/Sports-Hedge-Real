import {
  FEATURED_AWAY_TEAM_ID,
  FEATURED_FIXTURE_ID,
  FEATURED_HOME_TEAM_ID,
  FEATURED_KICKOFF_ISO,
  FEATURED_KICKOFF_LABEL,
} from "./ids";
import type { DataClass } from "./types";

export const DEMO_DATA_CLASS: DataClass = "DEMO_FIXTURE";

export const DEMO_BANNER = "DEMO / FIXTURE DATA";

export const PAPER_MODE_LABEL = "PAPER MODE · NO EXECUTION";

export const FEATURED_FIXTURE = {
  fixtureId: FEATURED_FIXTURE_ID,
  competition: "Premier League",
  competitionId: "premier-league" as const,
  venue: "Emirates Stadium",
  kickoffIso: FEATURED_KICKOFF_ISO,
  kickoffLabel: FEATURED_KICKOFF_LABEL,
  kickoffLocal: "Sat 12 Sep · 12:30",
  homeTeamId: FEATURED_HOME_TEAM_ID,
  awayTeamId: FEATURED_AWAY_TEAM_ID,
  homeName: "Arsenal",
  awayName: "Fulham",
  homeManager: "Mikel Arteta",
  awayManager: "Marco Silva",
  homeManagerPhase: "ESTABLISHED" as const,
  awayManagerPhase: "ESTABLISHED" as const,
  featured: true,
  dataClass: DEMO_DATA_CLASS,
};

export const RESEARCH_BROWSE = [
  { href: "/research", label: "Research Home", icon: "RH", description: "Odds-weighted value, fixtures and browse paths." },
  { href: "/matchday", label: "Matchday", icon: "MD", description: "Kickoff board and per-fixture scenario shortlists." },
  { href: "/teams", label: "Team Explorer", icon: "TM", description: "Club directory and Scenario Response Profiles." },
  { href: "/teams/arsenal", label: "Arsenal dashboard", icon: "ARS", description: "Richest team example for the walkthrough." },
  { href: "/scenario-lab", label: "Scenario Lab", icon: "SL", description: "Team × scenario SRC matrix with era splits." },
  { href: "/scenario-planner", label: "Scenario Planner", icon: "PL", description: "Paper watch rules with an odds/value gate." },
  { href: "/trends", label: "Trends", icon: "TR", description: "Historical cohort behaviour (existing MI surface)." },
  { href: "/market-intelligence", label: "Market Intelligence", icon: "MI", description: "Event-driven price and liquidity movement." },
] as const;

export const HISTORICAL_SEAM = {
  dataClass: "HISTORICAL" as DataClass,
  title: "Historical repository",
  status: "Repository-derived coverage when the read API can open the SQLite files",
  note:
    "Premier League and Championship 2021/22–2025/26 facts and odds are merged. Counts come from `/research/historical/coverage`, not hardcoded product logic. Tenet 17 analogue scoring is UNAVAILABLE. Same-line opening→closing pairs are price movement; AH line changes are structural line shifts. Correlation/context only, never causation.",
};
