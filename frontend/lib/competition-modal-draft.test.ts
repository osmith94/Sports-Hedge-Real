import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import type { OperatorCompetitionOption, OperatorUniverseScope } from "./api";
import {
  applyCompetitionModalProps,
  competitionDraftChecked,
  competitionModalDraftFromScope,
  defaultCompetitionCodes,
  savedDefaultScopeCodes,
  selectedScopeCodes,
  shouldInitializeCompetitionModalDraft,
  splitUniverseDraft,
  toggleCompetitionDraft,
  type CompetitionModalLocalState,
} from "./competition-modal-draft";

const here = dirname(fileURLToPath(import.meta.url));
const modalSource = readFileSync(join(here, "..", "components", "football-competitions-modal.tsx"), "utf8");

const UEFA = {
  champions: "champions_league",
  europa: "europa_league",
  conference: "conference_league",
} as const;

function option(
  code: string,
  group_id: string,
  group_label: string,
  extras: Partial<OperatorCompetitionOption> = {},
): OperatorCompetitionOption {
  return {
    code,
    display_name: code,
    selector_label: extras.selector_label ?? code,
    group_id,
    group_label,
    default_selected: extras.default_selected ?? false,
    selectable: extras.selectable ?? true,
    ...extras,
  };
}

function catalog(): OperatorCompetitionOption[] {
  return [
    option("premier_league", "england", "England", {
      selector_label: "Premier League",
      default_selected: true,
    }),
    option(UEFA.champions, "uefa", "UEFA", {
      selector_label: "UEFA Champions League",
      default_selected: true,
    }),
    option(UEFA.europa, "uefa", "UEFA", { selector_label: "UEFA Europa League" }),
    option(UEFA.conference, "uefa", "UEFA", { selector_label: "UEFA Conference League" }),
  ];
}

function scope(overrides: Partial<OperatorUniverseScope> = {}): OperatorUniverseScope {
  const selected = overrides.selected_competition_codes ?? ["premier_league", UEFA.champions];
  return {
    sport: "football",
    saved_default_competition_codes: ["premier_league", UEFA.champions],
    saved_default_count: 2,
    is_session_override: false,
    scope_version: 1,
    registry_version: 3,
    source: "operator",
    updated_at: "2026-09-20T12:00:00Z",
    needs_first_run_confirmation: false,
    new_competitions_available: false,
    catalog: catalog(),
    ...overrides,
    selected_competition_codes: selected,
    selected_count: overrides.selected_count ?? selected.length,
  };
}

function closedState(): CompetitionModalLocalState {
  return { open: false, query: "", draft: [], saveAsDefault: false };
}

/** Parent rerender of FootballCompetitionsModal with possibly-new live-status `scope`. */
function rerender(
  local: CompetitionModalLocalState,
  open: boolean,
  nextScope: OperatorUniverseScope | null,
): CompetitionModalLocalState {
  return applyCompetitionModalProps(local, { open, scope: nextScope });
}

describe("Football competitions modal draft vs live-status refresh", () => {
  it("initializes draft only on a closed → open transition", () => {
    expect(shouldInitializeCompetitionModalDraft(false, true)).toBe(true);
    expect(shouldInitializeCompetitionModalDraft(true, true)).toBe(false);
    expect(shouldInitializeCompetitionModalDraft(true, false)).toBe(false);
    expect(shouldInitializeCompetitionModalDraft(false, false)).toBe(false);
  });

  it("keeps unsaved multi-UEFA checks when the open modal rerenders with a fresh scope object", () => {
    const persisted = scope();
    let local = rerender(closedState(), true, persisted);

    expect(local.draft).toEqual(["premier_league", UEFA.champions]);
    expect(competitionDraftChecked(local.draft, UEFA.champions)).toBe(true);
    expect(competitionDraftChecked(local.draft, UEFA.europa)).toBe(false);
    expect(competitionDraftChecked(local.draft, UEFA.conference)).toBe(false);

    local = {
      ...local,
      query: "UEFA",
      saveAsDefault: true,
      draft: toggleCompetitionDraft(
        toggleCompetitionDraft(local.draft, UEFA.europa),
        UEFA.conference,
      ),
    };
    expect(local.draft).toEqual(["premier_league", UEFA.champions, UEFA.europa, UEFA.conference]);

    const polledSameSelection = scope({
      updated_at: "2026-09-20T12:00:08Z",
      scope_version: 1,
      generation_scope_version: 4,
    });
    const polledAuthoritativeRevert = scope({
      updated_at: "2026-09-20T12:00:12Z",
      selected_competition_codes: ["premier_league", UEFA.champions],
      selected_count: 2,
      manual_universe_state: "running",
    });

    local = rerender(local, true, polledSameSelection);
    local = rerender(local, true, polledAuthoritativeRevert);

    expect(local.open).toBe(true);
    expect(local.query).toBe("UEFA");
    expect(local.saveAsDefault).toBe(true);
    expect(local.draft).toEqual(["premier_league", UEFA.champions, UEFA.europa, UEFA.conference]);
    expect(competitionDraftChecked(local.draft, UEFA.champions)).toBe(true);
    expect(competitionDraftChecked(local.draft, UEFA.europa)).toBe(true);
    expect(competitionDraftChecked(local.draft, UEFA.conference)).toBe(true);
  });

  it("submits the full unsaved multi-select list on Apply after a live-status rerender", () => {
    const persisted = scope();
    let local = rerender(closedState(), true, persisted);
    local = {
      ...local,
      draft: toggleCompetitionDraft(toggleCompetitionDraft(local.draft, UEFA.europa), UEFA.conference),
      saveAsDefault: true,
    };
    local = rerender(
      local,
      true,
      scope({ updated_at: "2026-09-20T12:01:00Z", selected_competition_codes: persisted.selected_competition_codes }),
    );

    const applied = { codes: local.draft, runUniverseNow: false, saveAsDefault: local.saveAsDefault };
    expect(applied.codes).toEqual(["premier_league", UEFA.champions, UEFA.europa, UEFA.conference]);
    expect(applied.saveAsDefault).toBe(true);
    expect(new Set(applied.codes).has(UEFA.champions)).toBe(true);
    expect(new Set(applied.codes).has(UEFA.europa)).toBe(true);
    expect(new Set(applied.codes).has(UEFA.conference)).toBe(true);
  });

  it("reloads the latest authoritative scope after cancel and reopen", () => {
    const first = scope();
    let local = rerender(closedState(), true, first);
    local = { ...local, query: "UEFA", draft: toggleCompetitionDraft(local.draft, UEFA.europa) };

    local = rerender(local, false, first);
    expect(local.open).toBe(false);
    expect(local.draft).toEqual(["premier_league", UEFA.champions, UEFA.europa]);

    const laterAuthoritative = scope({
      selected_competition_codes: ["premier_league", UEFA.champions, UEFA.europa],
      selected_count: 3,
      scope_version: 2,
      updated_at: "2026-09-20T12:05:00Z",
      is_session_override: true,
    });
    local = rerender(local, true, laterAuthoritative);

    expect(local.query).toBe("");
    expect(local.saveAsDefault).toBe(false);
    expect(local.draft).toEqual(["premier_league", UEFA.champions, UEFA.europa]);
    expect(competitionDraftChecked(local.draft, UEFA.conference)).toBe(false);
  });

  it("preserves an explicit empty persisted selection and otherwise uses default-selected codes", () => {
    const rows = catalog();
    expect(defaultCompetitionCodes(rows)).toEqual(["premier_league", UEFA.champions]);

    const emptyOpened = rerender(closedState(), true, scope({ selected_competition_codes: [] }));
    expect(emptyOpened.draft).toEqual([]);

    const missingCodes = { ...scope() };
    delete (missingCodes as { selected_competition_codes?: string[] }).selected_competition_codes;
    expect(competitionModalDraftFromScope(missingCodes).draft).toEqual(["premier_league", UEFA.champions]);
  });
});

describe("FootballCompetitionsModal wiring contract", () => {
  it("gates draft reset on a genuine open transition rather than every new scope object", () => {
    expect(modalSource).toContain("shouldInitializeCompetitionModalDraft(wasOpenRef.current, open)");
    expect(modalSource).toContain("competitionModalDraftFromScope(scope)");
    expect(modalSource).toContain("wasOpenRef.current = open");
    expect(modalSource).not.toMatch(/if \(!open\) return;\s*setQuery\(""\);/);
  });

  it("binds checkboxes and Apply to the local draft so multiple UEFA rows can stay selected", () => {
    expect(modalSource).toContain("const selectedSet = new Set(draft)");
    expect(modalSource).toContain("checked={checked}");
    expect(modalSource).toContain("toggleCompetitionDraft(current, row.code)");
    expect(modalSource).toContain("void onApply(draft, false, saveAsDefault)");
    expect(modalSource).toContain("void onApply(draft, true, saveAsDefault)");
    expect(modalSource).toContain('void onApply(savedDefault, false, false)');
    expect(modalSource).toContain('"uefa"');
    expect(modalSource).toContain('"outrights"');
    expect(modalSource).toContain("Discovery scope");
  });
});

describe("COMPETITION_SEASON picker rows in the same UNIVERSE draft", () => {
  const seasonCatalog: OperatorCompetitionOption[] = [
    ...catalog(),
    option("epl_2026_27_champion", "outrights", "Outrights / season markets", {
      selector_label: "Premier League 2026-27 · Champion",
      market_scope: "COMPETITION_SEASON",
      observation_only: true,
      paper_executable: false,
    }),
    option("nfl_2026_super_bowl_champion", "outrights", "Outrights / season markets", {
      selector_label: "NFL 2026 · Super Bowl Champion",
      market_scope: "COMPETITION_SEASON",
      observation_only: true,
      paper_executable: false,
    }),
    option("epl_2026_27_top_scorer", "outrights", "Outrights / season markets", {
      selector_label: "Premier League 2026-27 · Top Scorer",
      market_scope: "COMPETITION_SEASON",
      observation_only: true,
      paper_executable: false,
      unavailable_reason: "Observation-only · not executable while cross-venue equivalence remains blocked",
    }),
  ];

  function seasonScope(overrides: Partial<OperatorUniverseScope> = {}): OperatorUniverseScope {
    return scope({
      catalog: seasonCatalog,
      selected_season_scope_codes: [],
      saved_default_season_scope_codes: [],
      ...overrides,
    });
  }

  it("keeps season codes out of fixture defaults and splits current vs saved lists", () => {
    expect(defaultCompetitionCodes(seasonCatalog)).toEqual(["premier_league", UEFA.champions]);
    const split = splitUniverseDraft(
      ["premier_league", "epl_2026_27_champion", "epl_2026_27_top_scorer"],
      seasonCatalog,
    );
    expect(split.selected_competition_codes).toEqual(["premier_league"]);
    expect(split.selected_season_scope_codes).toEqual([
      "epl_2026_27_champion",
      "epl_2026_27_top_scorer",
    ]);
    const current = seasonScope({
      selected_competition_codes: ["premier_league"],
      selected_season_scope_codes: ["epl_2026_27_champion"],
      saved_default_competition_codes: ["premier_league", UEFA.champions],
      saved_default_season_scope_codes: [],
    });
    expect(selectedScopeCodes(current)).toEqual(["premier_league", "epl_2026_27_champion"]);
    expect(savedDefaultScopeCodes(current, seasonCatalog)).toEqual(["premier_league", UEFA.champions]);
    expect(competitionModalDraftFromScope(current).draft).toEqual([
      "premier_league",
      "epl_2026_27_champion",
    ]);
  });

  it("reloads season session selection after cancel and reopen without dropping unsaved fixture checks while open", () => {
    const opened = seasonScope({
      selected_competition_codes: ["premier_league"],
      selected_season_scope_codes: ["nfl_2026_super_bowl_champion"],
    });
    let local = rerender(closedState(), true, opened);
    expect(local.draft).toEqual(["premier_league", "nfl_2026_super_bowl_champion"]);
    local = {
      ...local,
      draft: toggleCompetitionDraft(local.draft, "epl_2026_27_top_scorer"),
    };
    local = rerender(
      local,
      true,
      seasonScope({
        selected_competition_codes: ["premier_league"],
        selected_season_scope_codes: ["nfl_2026_super_bowl_champion"],
        updated_at: "2026-09-21T08:01:00Z",
      }),
    );
    expect(local.draft).toEqual([
      "premier_league",
      "nfl_2026_super_bowl_champion",
      "epl_2026_27_top_scorer",
    ]);
    local = rerender(local, false, opened);
    const later = seasonScope({
      selected_competition_codes: ["premier_league"],
      selected_season_scope_codes: ["epl_2026_27_top_scorer"],
      is_session_override: true,
    });
    local = rerender(local, true, later);
    expect(local.draft).toEqual(["premier_league", "epl_2026_27_top_scorer"]);
    expect(competitionDraftChecked(local.draft, "epl_2026_27_top_scorer")).toBe(true);
  });
});
