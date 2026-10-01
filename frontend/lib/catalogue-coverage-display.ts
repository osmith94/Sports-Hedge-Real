import {
  DiscoveredFixture,
  FixtureArchetypeCoverage,
  FixtureCatalogueCoverage,
  LiveRefreshStatus,
} from "./api";

const STATE_LABELS: Record<string, string> = {
  approved_equivalent: "APPROVED_EQUIVALENT",
  paper_assumed_equivalent: "REGISTERED_EQUIVALENT",
  approved_parameter_mismatch: "APPROVED_PARAMETER_MISMATCH",
  known_contradiction: "KNOWN_CONTRADICTION",
  review_required: "REVIEW_REQUIRED",
  unsupported: "UNSUPPORTED",
  venue_unavailable: "VENUE_UNAVAILABLE",
  not_listed: "NOT_LISTED",
};

export function coverageStateLabel(state: string): string {
  return STATE_LABELS[state] ?? state.replaceAll("_", " ").toUpperCase();
}

export function coverageRowLabel(row: FixtureArchetypeCoverage): string {
  const label = (row.display_label || row.archetype).padEnd(16, " ");
  return `${label}${coverageStateLabel(row.state)} — ${row.reason.replaceAll("_", " ")}`;
}

export function fixtureCoverageRows(
  item: DiscoveredFixture | { catalogue_coverage?: FixtureCatalogueCoverage | null },
): FixtureArchetypeCoverage[] {
  return item.catalogue_coverage?.rows ?? [];
}

function diagnosticsCatalogueByArchetype(
  status: LiveRefreshStatus | null,
): Record<string, Record<string, number>> | null {
  const sources = [status?.universe?.last_diagnostics, status?.hot?.last_diagnostics];
  for (const diagnostics of sources) {
    const matching =
      diagnostics && typeof diagnostics === "object"
        ? (
            diagnostics as {
              matching_coverage?: {
                catalogue_by_archetype?: Record<string, Record<string, number>>;
              };
            }
          ).matching_coverage
        : undefined;
    if (matching?.catalogue_by_archetype) {
      return matching.catalogue_by_archetype;
    }
  }
  return null;
}

export function universeArchetypeSummary(
  status: LiveRefreshStatus | null,
): Record<string, Record<string, number>> {
  const fromDiagnostics = diagnosticsCatalogueByArchetype(status);
  if (fromDiagnostics) return fromDiagnostics;
  const totals: Record<string, Record<string, number>> = {};
  for (const fixture of status?.discovered_fixtures ?? []) {
    for (const row of fixtureCoverageRows(fixture)) {
      const bucket = (totals[row.archetype] ??= {});
      bucket[row.state] = (bucket[row.state] ?? 0) + 1;
    }
  }
  return totals;
}

export function universeArchetypeSummaryLines(status: LiveRefreshStatus | null): string[] {
  const summary = universeArchetypeSummary(status);
  return Object.entries(summary).map(([archetype, counts]) => {
    const approved = counts.approved_equivalent ?? 0;
    const paperAssumed = counts.paper_assumed_equivalent ?? 0;
    const review = counts.review_required ?? 0;
    const unsupported = counts.unsupported ?? 0;
    const unavailable = counts.venue_unavailable ?? 0;
    return (
      `${archetype}: ${approved} approved · ${paperAssumed} registered equivalent · ` +
      `${review} review · ${unsupported} unsupported · ${unavailable} unavailable`
    );
  });
}
