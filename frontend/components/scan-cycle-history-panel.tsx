"use client";

import { useState } from "react";

import {
  PaperScanCycleRecord,
  ScanCycleDiagnosticResponse,
  getPaperScanCycleReport,
} from "../lib/api";
import {
  SCAN_CYCLE_COPY,
  SCAN_CYCLE_EMPTY,
  SCAN_CYCLE_HEADERS,
  SCAN_CYCLE_REPORT_ACTION,
  SCAN_CYCLE_REPORT_EMPTY,
  SCAN_CYCLE_TITLE,
  SCAN_CYCLE_UNAVAILABLE,
  scanCycleBadgeLabel,
  scanCycleDiagnosticLines,
  scanCycleLatestSummary,
  scanCycleRows,
} from "../lib/scan-cycle-history-display";
import { useHydratedNowMs } from "./hydrated-relative-time";

export function ScanCycleHistoryPanel({
  cycles,
  available,
}: {
  cycles: PaperScanCycleRecord[];
  available: boolean;
}) {
  const nowMs = useHydratedNowMs();
  const rows = available ? scanCycleRows(cycles, nowMs) : [];
  const badge = scanCycleBadgeLabel(available, cycles);
  const latest = scanCycleLatestSummary(available, cycles);
  const loadedLabel = `${rows.length} loaded cycle${rows.length === 1 ? "" : "s"}`;

  return (
    <section className="scan-cycle-history-panel">
      <details className="discovery-disclosure scan-cycle-history-disclosure">
        <summary className="discovery-summary">
          <span className="discovery-chevron" aria-hidden="true" />
          <span className="audit-summary-copy">
            {SCAN_CYCLE_TITLE} · {latest} · {loadedLabel}
          </span>
          <span className={available ? "status-badge" : "demo-chip"}>{badge}</span>
          <span className="discovery-toggle discovery-toggle-show">Show history</span>
          <span className="discovery-toggle discovery-toggle-hide">Hide history</span>
        </summary>
        <section className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">{SCAN_CYCLE_TITLE}</div>
              <div className="panel-meta">{SCAN_CYCLE_COPY}</div>
            </div>
            <span className={available ? "status-badge" : "demo-chip"}>{badge}</span>
          </div>
          {!available ? <div className="empty-live-compact">{SCAN_CYCLE_UNAVAILABLE}</div> : null}
          {available && rows.length === 0 ? (
            <div className="empty-live-compact">{SCAN_CYCLE_EMPTY}</div>
          ) : null}
          {available && rows.length > 0 ? <CycleTable rows={rows} /> : null}
        </section>
      </details>
    </section>
  );
}

function CycleTable({ rows }: { rows: ReturnType<typeof scanCycleRows> }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState<ScanCycleDiagnosticResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  async function viewReport(cycleId: string) {
    if (selectedId === cycleId && loaded?.cycle_id === cycleId) {
      setSelectedId(null);
      return;
    }
    setSelectedId(cycleId);
    setLoadingId(cycleId);
    setLoadError(null);
    try {
      const response = await getPaperScanCycleReport(cycleId);
      setLoaded(response);
    } catch (error) {
      setLoaded(null);
      setLoadError(error instanceof Error ? error.message : "Cycle diagnostic request failed");
    } finally {
      setLoadingId(null);
    }
  }

  const lines =
    loaded && loaded.cycle_id === selectedId && loaded.available
      ? scanCycleDiagnosticLines(loaded.report)
      : [];

  return (
    <div>
      <div className="table-wrap">
        <table className="scan-cycle-table">
          <thead>
            <tr>
              {SCAN_CYCLE_HEADERS.map((header) => (
                <th key={header}>{header}</th>
              ))}
              <th>Report</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td className="muted">{row.completedLabel}</td>
                <td>{row.laneLabel}</td>
                <td>{row.durationLabel}</td>
                <td>{row.fixtureLabel}</td>
                <td>{row.evaluatedLabel}</td>
                <td>{row.matchedLabel}</td>
                <td>{row.paperDecisionLabel}</td>
                <td>{row.qualifyingLabel}</td>
                <td className={row.degraded ? undefined : "muted"}>{row.healthLabel}</td>
                <td>
                  <button type="button" className="sort-header" onClick={() => void viewReport(row.id)}>
                    {selectedId === row.id ? "Hide report" : SCAN_CYCLE_REPORT_ACTION}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {selectedId ? (
        <div className="panel-meta scan-cycle-report">
          <div>Cycle diagnostic. Not live quotes. Not a raw provider response.</div>
          {loadingId === selectedId ? <div>Loading stored diagnostic…</div> : null}
          {loadError ? <div>{loadError}</div> : null}
          {loaded && loaded.cycle_id === selectedId && !loaded.available ? (
            <div>{loaded.note || SCAN_CYCLE_REPORT_EMPTY}</div>
          ) : null}
          {lines.length > 0 ? (
            <ul>
              {lines.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
