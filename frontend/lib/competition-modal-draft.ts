import type { OperatorCompetitionOption, OperatorUniverseScope } from "./api";

export type CompetitionModalLocalState = {
  open: boolean;
  query: string;
  draft: string[];
  saveAsDefault: boolean;
};

export function defaultCompetitionCodes(catalog: OperatorCompetitionOption[]): string[] {
  return catalog.filter((row) => row.default_selected && row.selectable).map((row) => row.code);
}

export function supportedCompetitionCodes(catalog: OperatorCompetitionOption[]): string[] {
  return catalog.filter((row) => row.selectable).map((row) => row.code);
}

export function shouldInitializeCompetitionModalDraft(wasOpen: boolean, open: boolean): boolean {
  return open && !wasOpen;
}

export function competitionModalDraftFromScope(scope: OperatorUniverseScope | null): Omit<
  CompetitionModalLocalState,
  "open"
> {
  const catalog = scope?.catalog ?? [];
  return {
    query: "",
    draft: [...(scope?.selected_competition_codes ?? defaultCompetitionCodes(catalog))],
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
