import type { OperatorCompetitionOption, OperatorUniverseScope } from "./api";

export type CompetitionModalLocalState = {
  open: boolean;
  query: string;
  draft: string[];
  saveAsDefault: boolean;
};

export function isSeasonScopeOption(row: OperatorCompetitionOption): boolean {
  return row.market_scope === "COMPETITION_SEASON";
}

export function defaultCompetitionCodes(catalog: OperatorCompetitionOption[]): string[] {
  return catalog.filter((row) => row.default_selected && row.selectable).map((row) => row.code);
}

export function supportedCompetitionCodes(catalog: OperatorCompetitionOption[]): string[] {
  return catalog.filter((row) => row.selectable).map((row) => row.code);
}

export function selectedScopeCodes(scope: OperatorUniverseScope | null): string[] {
  if (!scope) return [];
  return [
    ...(scope.selected_competition_codes ?? []),
    ...(scope.selected_season_scope_codes ?? []),
  ];
}

export function savedDefaultScopeCodes(
  scope: OperatorUniverseScope | null,
  catalog: OperatorCompetitionOption[],
): string[] {
  if (scope?.saved_default_competition_codes) {
    return [
      ...scope.saved_default_competition_codes,
      ...(scope.saved_default_season_scope_codes ?? []),
    ];
  }
  return defaultCompetitionCodes(catalog);
}

export function splitUniverseDraft(
  codes: readonly string[],
  catalog: OperatorCompetitionOption[],
): { selected_competition_codes: string[]; selected_season_scope_codes: string[] } {
  const season = new Set(catalog.filter(isSeasonScopeOption).map((row) => row.code));
  return {
    selected_competition_codes: codes.filter((code) => !season.has(code)),
    selected_season_scope_codes: codes.filter((code) => season.has(code)),
  };
}

export function shouldInitializeCompetitionModalDraft(wasOpen: boolean, open: boolean): boolean {
  return open && !wasOpen;
}

export function competitionModalDraftFromScope(scope: OperatorUniverseScope | null): Omit<
  CompetitionModalLocalState,
  "open"
> {
  const catalog = scope?.catalog ?? [];
  const fixtureCodes =
    scope?.selected_competition_codes ?? defaultCompetitionCodes(catalog);
  const seasonCodes = scope?.selected_season_scope_codes ?? [];
  return {
    query: "",
    draft: [...fixtureCodes, ...seasonCodes],
    saveAsDefault: Boolean(scope?.needs_first_run_confirmation),
  };
}

/** Apply a parent rerender. Initializes draft only on closed → open, never on live-status scope identity changes. */
export function applyCompetitionModalProps(
  previous: CompetitionModalLocalState,
  props: { open: boolean; scope: OperatorUniverseScope | null },
): CompetitionModalLocalState {
  if (!shouldInitializeCompetitionModalDraft(previous.open, props.open)) {
    return { ...previous, open: props.open };
  }
  return { open: true, ...competitionModalDraftFromScope(props.scope) };
}

export function toggleCompetitionDraft(draft: string[], code: string): string[] {
  return draft.includes(code) ? draft.filter((item) => item !== code) : [...draft, code];
}

export function competitionDraftChecked(draft: readonly string[], code: string): boolean {
  return draft.includes(code);
}
