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
  game_winner: "Game Winner",
  point_spread: "Spread",
  total_points: "Total Points",
};

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

function familyDisplayLabel(trade: PaperTrade): string {
  if (trade.market_family) {
    return FAMILY_LABELS[trade.market_family] ?? humanizeToken(trade.market_family);
  }
  if (trade.market_label) return trade.market_label;
  return "—";
}

function compactOutcomeLabel(outcome: string, trade: PaperTrade): string {
  const label = outcome.replaceAll("_", " ").toUpperCase();
  const stored = formatStoredLine(trade.line);
  if (stored) return `${label} ${stored}`;
  if (trade.market_family && LINE_PARAMETER_FAMILIES.has(trade.market_family)) {
    return `${label} n.a.`;
  }
  return label;
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
