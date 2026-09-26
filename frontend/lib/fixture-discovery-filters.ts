type DiscoveryFilterable = {
  sport?: string | null;
  competition?: string | null;
};

/** Display-only Fixture Discovery filters. They do not read or write scanner scope. */

export const ALL_DISCOVERY_FILTER = "all";
export const UNKNOWN_SPORT = "unknown";

const SPORT_LABELS: Record<string, string> = {
  football: "Football",
  american_football: "American Football",
  basketball: "Basketball",
  baseball: "Baseball",
  tennis: "Tennis",
  boxing: "Boxing",
  mma: "MMA",
  motorsport: "Motorsport",
  ice_hockey: "Ice Hockey",
  unknown: "Unknown",
};

export type DiscoveryFilterSelection = {
  sport: string;
  competition: string;
};

export type SportFilterOption = {
  token: string;
  label: string;
  count: number;
};

export type CompetitionFilterOption = {
  key: string;
  label: string;
  count: number;
};

export const DEFAULT_DISCOVERY_FILTERS: DiscoveryFilterSelection = {
  sport: ALL_DISCOVERY_FILTER,
  competition: ALL_DISCOVERY_FILTER,
};

export function fixtureSportToken(item: DiscoveryFilterable): string {
  const token = (item.sport ?? "").trim().toLowerCase();
  if (!token || token === UNKNOWN_SPORT || !(token in SPORT_LABELS)) return UNKNOWN_SPORT;
  return token;
}

export function sportFilterLabel(token: string): string {
  return SPORT_LABELS[token] ?? SPORT_LABELS[UNKNOWN_SPORT];
}

export function competitionFilterKey(item: DiscoveryFilterable): string {
  const label = (item.competition ?? "").trim();
  return label || "Unknown";
}

export function sportFilterOptions(fixtures: readonly DiscoveryFilterable[]): SportFilterOption[] {
  const counts = new Map<string, number>();
  for (const item of fixtures) {
    const token = fixtureSportToken(item);
    counts.set(token, (counts.get(token) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([token, count]) => ({ token, label: sportFilterLabel(token), count }))
    .sort((left, right) => {
      if (left.token === UNKNOWN_SPORT) return 1;
      if (right.token === UNKNOWN_SPORT) return -1;
      return right.count - left.count || left.label.localeCompare(right.label);
    });
}

export function competitionFilterOptions(
  fixtures: readonly DiscoveryFilterable[],
  sport: string = ALL_DISCOVERY_FILTER,
): CompetitionFilterOption[] {
  const counts = new Map<string, number>();
  for (const item of fixtures) {
    if (sport !== ALL_DISCOVERY_FILTER && fixtureSportToken(item) !== sport) continue;
    const key = competitionFilterKey(item);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([key, count]) => ({ key, label: key, count }))
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label));
}

export function applyFixtureDiscoveryFilters<T extends DiscoveryFilterable>(
  fixtures: readonly T[],
  selection: DiscoveryFilterSelection = DEFAULT_DISCOVERY_FILTERS,
): T[] {
  const sport = selection.sport || ALL_DISCOVERY_FILTER;
  const competition = selection.competition || ALL_DISCOVERY_FILTER;
  return fixtures.filter((item) => {
    if (sport !== ALL_DISCOVERY_FILTER && fixtureSportToken(item) !== sport) return false;
    if (competition !== ALL_DISCOVERY_FILTER && competitionFilterKey(item) !== competition) return false;
    return true;
  });
}
