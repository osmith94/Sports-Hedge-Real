"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { OperatorCompetitionOption, OperatorUniverseScope } from "../lib/api";
import {
  competitionModalDraftFromScope,
  defaultCompetitionCodes,
  savedDefaultScopeCodes,
  selectedScopeCodes,
  shouldInitializeCompetitionModalDraft,
  supportedCompetitionCodes,
  toggleCompetitionDraft,
} from "../lib/competition-modal-draft";

const GROUP_ORDER = [
  "outrights",
  "uefa",
  "england",
  "spain",
  "germany",
  "italy",
  "france",
  "netherlands",
  "portugal",
  "scotland",
  "belgium",
  "turkey",
  "usa_canada",
  "mexico",
  "brazil",
  "argentina",
  "south_america",
  "saudi_arabia",
  "japan",
  "south_africa",
  "international",
    "nfl",
    "nba",
    "college_basketball",
];

type FootballCompetitionsModalProps = {
  open: boolean;
  scope: OperatorUniverseScope | null;
  scannerStopped?: boolean;
  saving?: boolean;
  errorMessage?: string | null;
  onClose: () => void;
  onApply: (codes: string[], runUniverseNow: boolean, saveAsDefault: boolean) => Promise<void> | void;
};

function namesFor(codes: string[], catalog: OperatorCompetitionOption[]): string {
  const labels = new Map(catalog.map((row) => [row.code, row.selector_label]));
  const named = codes.map((code) => labels.get(code) ?? code);
  if (named.length <= 3) return named.join(", ");
  return `${named.slice(0, 3).join(", ")} +${named.length - 3}`;
}

export function FootballCompetitionsModal({
  open,
  scope,
  scannerStopped = false,
  saving = false,
  errorMessage = null,
  onClose,
  onApply,
}: FootballCompetitionsModalProps) {
  const catalog = scope?.catalog ?? [];
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState<string[]>(selectedScopeCodes(scope));
  const [saveAsDefault, setSaveAsDefault] = useState(false);
  const wasOpenRef = useRef(false);

  useEffect(() => {
    // HOT/BACKGROUND/UNIVERSE polling replaces `scope` with a new object while the
    // dialog stays open. Reinitialize only on a genuine closed → open transition.
    if (shouldInitializeCompetitionModalDraft(wasOpenRef.current, open)) {
      const next = competitionModalDraftFromScope(scope);
      setQuery(next.query);
      setDraft(next.draft);
      setSaveAsDefault(next.saveAsDefault);
    }
    wasOpenRef.current = open;
  }, [open, scope]);

  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const byGroup = new Map<string, { label: string; rows: OperatorCompetitionOption[] }>();
    for (const row of catalog) {
      const haystack = `${row.selector_label} ${row.display_name} ${row.group_label}`.toLowerCase();
      if (needle && !haystack.includes(needle)) continue;
      const existing = byGroup.get(row.group_id);
      if (existing) {
        existing.rows.push(row);
      } else {
        byGroup.set(row.group_id, { label: row.group_label, rows: [row] });
      }
    }
    const extras = [...byGroup.keys()].filter((id) => !GROUP_ORDER.includes(id));
    return [...GROUP_ORDER, ...extras].flatMap((id) => {
      const group = byGroup.get(id);
      return group ? [{ id, ...group }] : [];
    });
  }, [catalog, query]);

  if (!open) return null;

  const selectedSet = new Set(draft);
  const selectedCount = draft.length;
  const firstRun = Boolean(scope?.needs_first_run_confirmation);
  const savedDefault = savedDefaultScopeCodes(scope, catalog);
  const savedCount = savedDefault.length;
  const activeCodes = selectedScopeCodes(scope).length ? selectedScopeCodes(scope) : draft;

  function toggle(row: OperatorCompetitionOption) {
    if (!row.selectable) return;
    setDraft((current) => toggleCompetitionDraft(current, row.code));
  }

  return (
    <div className="competition-modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="competition-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="football-competitions-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="competition-modal-header">
          <div>
            <div className="competition-modal-kicker">UNIVERSE</div>
            <h2 id="football-competitions-title">Discovery scope</h2>
            <p>
              {firstRun
                ? "Confirm the default UNIVERSE discovery scope. Checking Save this selection as my default stores it for restart."
                : "Fixture competitions and season markets control UNIVERSE discovery. Venue tickers stay backend-only."}
            </p>
            <p className="competition-modal-scope-meta">
              Active: {namesFor(activeCodes, catalog)} ({scope?.selected_count ?? selectedCount})
              {" · "}
              Saved default: {namesFor(savedDefault, catalog)} ({savedCount})
              {scope?.is_session_override ? " · session override" : ""}
            </p>
          </div>
          <button className="scan-button-secondary" type="button" onClick={onClose}>
            Cancel
          </button>
        </div>
        <label className="scan-field competition-modal-search">
          <span>Search</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Premier League, Super Bowl, MLS…"
            aria-label="Search UNIVERSE discovery scopes"
          />
        </label>
        <div className="competition-modal-shortcuts">
          <button
            className="scan-button-secondary"
            type="button"
            onClick={() => setDraft(defaultCompetitionCodes(catalog))}
          >
            Select defaults
          </button>
          <button
            className="scan-button-secondary"
            type="button"
            onClick={() => setDraft(supportedCompetitionCodes(catalog))}
          >
            Select all supported
          </button>
          <button className="scan-button-secondary" type="button" onClick={() => setDraft([])}>
            Clear all
          </button>
          <button
            className="scan-button-secondary"
            type="button"
            disabled={saving}
            onClick={() => void onApply(savedDefault, false, false)}
          >
            Restore saved default
          </button>
          <span className="competition-modal-count">{selectedCount} selected</span>
        </div>
        <div className="competition-modal-groups">
          {groups.map((group) => (
            <section key={group.id} className="competition-modal-group">
              <h3>{group.label}</h3>
              {group.rows.map((row) => {
                const checked = selectedSet.has(row.code);
                return (
                  <label
                    key={row.code}
                    className={
                      row.selectable
                        ? "competition-modal-row"
                        : "competition-modal-row competition-modal-row-disabled"
                    }
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!row.selectable}
                      onChange={() => toggle(row)}
                      aria-label={row.selector_label}
                    />
                    <span>
                      <strong>{row.selector_label}</strong>
                      {row.unavailable_reason ? (
                        <em>{row.unavailable_reason}</em>
                      ) : null}
                    </span>
                  </label>
                );
              })}
            </section>
          ))}
        </div>
        <label className="competition-modal-save-default">
          <input
            type="checkbox"
            checked={saveAsDefault}
            onChange={(event) => setSaveAsDefault(event.target.checked)}
            aria-label="Save this selection as my default"
          />
          Save this selection as my default
        </label>
        {errorMessage ? (
          <div className="scan-note" role="alert">
            {errorMessage}
          </div>
        ) : null}
        <div className="competition-modal-actions">
          <button
            className="scan-button-secondary"
            type="button"
            disabled={saving}
            onClick={() => void onApply(draft, false, saveAsDefault)}
          >
            {saving ? "Saving…" : "Apply"}
          </button>
          <button
            className="scan-button"
            type="button"
            disabled={saving || scannerStopped}
            title={scannerStopped ? "Scanner stopped by operator" : undefined}
            onClick={() => void onApply(draft, true, saveAsDefault)}
          >
            Apply & Run UNIVERSE now
          </button>
        </div>
      </div>
    </div>
  );
}
