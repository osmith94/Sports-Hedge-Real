import { MarketFamily, PaperLegFillKind, PaperTrade, PaperTradeLeg } from "./api";
import { money, number } from "./format";
import { venueTitle } from "./fixture-inventory-display";

export const LINE_PARAMETER_FAMILIES = new Set<MarketFamily>([
  "total_goals",
  "asian_handicap",
  "team_total",
  "point_spread",
  "total_points",
]);

const FAMILY_LABELS: Record<string, string> = {
  match_result: "Match Result",
  both_teams_to_score: "BTTS",
  total_goals: "Total Goals",
  first_team_to_score: "First Team To Score",
  draw_no_bet: "Draw No Bet",
  asian_handicap: "Asian Handicap",
  team_total: "Team Total",
  game_winner: "Game winner",
  point_spread: "Point spread",
  total_points: "Total points",
};

export const NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT = "exceptional_settlement_mismatch_possible";

export const NFL_SETTLEMENT_CAVEAT_TEXT =
  "PAPER comparison is for a normal completed NFL game. Cancellation, suspension, and final-tie handling can differ across venues and must be resolved before any live execution. Automatic settlement fails closed on those exceptional cases.";

export const NBA_SETTLEMENT_CAVEAT_TEXT =
  "PAPER comparison is for a normal completed NBA game. Cancellation, suspension, and overtime/exceptional handling can differ across venues and must be resolved before any live execution. Automatic settlement fails closed on those exceptional cases.";

export const NCAAB_PAIR_UNAPPROVED = "ncaab_venue_pair_family_not_evidence_backed";

export const NCAAB_SETTLEMENT_CAVEAT_TEXT =
  "NCAAB PAPER comparison is not admitted. No venue-pair/family cell has NCAAB-specific settlement evidence. Automatic settlement stays fail-closed and cannot ignore a participating venue.";

function ncaabCompetition(value: string | null | undefined): boolean {
  const text = (value ?? "").trim().toLowerCase();
  if (!text) return false;
  if (text.includes("women")) return false;
  return (
    text === "ncaab" ||
    text === "ncaa men's basketball" ||
    text === "ncaa mens basketball" ||
    text.startsWith("ncaa men") ||
    text.includes("college basketball")
  );
}

export function isNcaabPaperTrade(
  trade: Pick<PaperTrade, "market_family" | "competition">,
): boolean {
  return ncaabCompetition(trade.competition);
}

export function isNflPaperTrade(
  trade: Pick<PaperTrade, "market_family" | "competition">,
): boolean {
  if (isNcaabPaperTrade(trade)) return false;
  return (trade.competition ?? "").trim().toUpperCase() === "NFL";
}

export function isNbaPaperTrade(
  trade: Pick<PaperTrade, "market_family" | "competition">,
): boolean {
  return (trade.competition ?? "").trim().toUpperCase() === "NBA";
}

export function tradeShowsNflSettlementCaveat(
  trade: Pick<PaperTrade, "market_family" | "competition" | "audit">,
): boolean {
  void trade;
  return false;
}

export function tradeShowsNbaSettlementCaveat(
  trade: Pick<PaperTrade, "market_family" | "competition" | "audit">,
): boolean {
  void trade;
  return false;
}

export function reasonsIncludeNflSettlementCaveat(reasons: string[] | null | undefined): boolean {
  return (reasons ?? []).some(
    (reason) =>
      reason === NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT ||
      reason.includes(NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT),
  );
}

export function formatStoredLine(line: string | number | null | undefined): string | null {
  if (line === null || line === undefined || line === "") return null;
  const text = String(line).trim();
  if (!text) return null;
  if (!/^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$/.test(text)) return null;
  if (text.includes(".")) {
    const trimmed = text.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
    return trimmed === "-0" ? "0" : trimmed;
  }
  return text;
}

export function formatCompactDecimalOdds(
  value: string | number | null | undefined,
): string {
  const parsed = number(value);
  if (parsed === null) return "—";
  return parsed.toFixed(3);
}

export function compactMarketHeading(trade: PaperTrade): string {
  const familyLabel = familyDisplayLabel(trade);
  const stored = formatStoredLine(trade.line);
  if (stored) return `${familyLabel} ${stored}`;
  if (trade.market_family && LINE_PARAMETER_FAMILIES.has(trade.market_family)) {
    return `${familyLabel} n.a.`;
  }
  return familyLabel;
}

export function compactFillKindLabel(kind: PaperLegFillKind | null | undefined): string | null {
  if (kind === "INTERNAL_SIMULATED") return "PAPER";
  if (kind === "PAPER_SIMULATED_EXTERNAL") return "SIMULATED";
  if (kind === "MANUAL_EXTERNAL") return "MANUAL";
  return null;
}

export function compactLegLine(trade: PaperTrade, leg: PaperTradeLeg): string {
  const venue = venueTitle(leg.venue);
  const outcome = compactOutcomeLabel(leg.outcome, trade);
  const odds = formatCompactDecimalOdds(leg.filled_odds ?? leg.displayed_odds);
  const stake = compactStakeLabel(leg);
  const fill = compactFillKindLabel(leg.fill_kind);
  return fill
    ? `${venue} · ${outcome} @ ${odds} · ${stake} · ${fill}`
    : `${venue} · ${outcome} @ ${odds} · ${stake}`;
}

export function compactLegLines(trade: PaperTrade): string[] {
  if (!trade.legs.length) return ["No legs recorded"];
  return trade.legs.map((leg) => compactLegLine(trade, leg));
}

export type LegListIdentity = {
  fill_id?: string | null;
  tranche_id?: string | null;
  venue?: string | null;
  outcome?: string | null;
  source_market_id?: string | null;
};

/** Stable list key. Identity first, index always last so repeated tranches stay distinct. */
export function paperLegListKey(leg: LegListIdentity, index: number): string {
  const identity = [leg.fill_id, leg.tranche_id, leg.venue, leg.outcome, leg.source_market_id]
    .filter((part) => part != null && String(part).length > 0)
    .map((part) => String(part))
    .join("|");
  return identity ? `${identity}|${index}` : `leg|${index}`;
}

function familyDisplayLabel(trade: PaperTrade): string {
  if (trade.market_family) {
    return FAMILY_LABELS[trade.market_family] ?? humanizeToken(trade.market_family);
  }
  if (trade.market_label) return trade.market_label;
  return "—";
}

function compactOutcomeLabel(outcome: string, trade: PaperTrade): string {
  const ncaabSide = ncaabSideExplanation(outcome, trade);
  if (ncaabSide) return ncaabSide;
  const nflSide = nflSideExplanation(outcome, trade);
  if (nflSide) return nflSide;
  const nbaSide = nbaSideExplanation(outcome, trade);
  if (nbaSide) return nbaSide;
  const label = outcome.replaceAll("_", " ").toUpperCase();
  const stored = formatStoredLine(trade.line);
  if (stored) return `${label} ${stored}`;
  if (trade.market_family && LINE_PARAMETER_FAMILIES.has(trade.market_family)) {
    return `${label} n.a.`;
  }
  return label;
}

export function ncaabSideExplanation(outcome: string, trade: PaperTrade): string | null {
  if (!isNcaabPaperTrade(trade)) return null;
  return sideExplanation(outcome, trade);
}

export function nbaSideExplanation(outcome: string, trade: PaperTrade): string | null {
  if (!isNbaPaperTrade(trade)) return null;
  return sideExplanation(outcome, trade);
}

export function nflSideExplanation(outcome: string, trade: PaperTrade): string | null {
  if (!isNflPaperTrade(trade)) return null;
  return sideExplanation(outcome, trade);
}

function sideExplanation(outcome: string, trade: PaperTrade): string | null {
  const token = outcome.trim().toLowerCase();
  const home = shortTeamName(trade.home_team);
  const away = shortTeamName(trade.away_team);
  if (trade.market_family === "game_winner") {
    if (token === "home") return `${home} · Game winner`;
    if (token === "away") return `${away} · Game winner`;
    return null;
  }
  const stored = formatStoredLine(trade.line);
  if (stored == null) return null;
  const line = Number(stored);
  if (!Number.isFinite(line)) return null;
  if (trade.market_family === "point_spread") {
    if (token === "home") return spreadCoverExplanation(home, line);
    if (token === "away") return spreadCoverExplanation(away, -line);
    return null;
  }
  if (trade.market_family === "total_points") {
    if (token === "over") {
      const ceiling = Math.trunc(line + 0.5);
      return `Over ${stored} · ${ceiling}+ combined points`;
    }
    if (token === "under") {
      const floor = Math.trunc(line - 0.5);
      return `Under ${stored} · ${floor} or fewer combined points`;
    }
  }
  return null;
}

function spreadCoverExplanation(team: string, signedLine: number): string {
  const magnitude = formatStoredLine(Math.abs(signedLine)) ?? String(Math.abs(signedLine));
  const signedText = signedLine > 0 ? `+${magnitude}` : signedLine < 0 ? `-${magnitude}` : magnitude;
  if (signedLine < 0) {
    const needed = Math.trunc(Math.abs(signedLine) + 0.5);
    return `${team} ${signedText} · must win by ${needed}+`;
  }
  if (signedLine > 0) {
    const allowed = Math.trunc(signedLine - 0.5);
    return `${team} ${signedText} · may lose by up to ${allowed}, or win`;
  }
  return `${team} ${signedText}`;
}

function shortTeamName(team: string | null | undefined): string {
  if (!team) return "Team";
  const parts = team.trim().split(/\s+/);
  return parts[parts.length - 1] || team;
}

function compactStakeLabel(leg: PaperTradeLeg): string {
  const ccy = leg.currency === "USD" ? "USD" : "GBP";
  if (leg.fill_kind === "UNFILLED") {
    return `requested ${money(leg.requested_stake, ccy)} unfilled`;
  }
  return money(leg.filled_stake, ccy);
}

function humanizeToken(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
}
