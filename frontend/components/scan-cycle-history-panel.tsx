"use client";

import { PaperScanCycleRecord } from "../lib/api";
import {
  SCAN_CYCLE_COPY,
  SCAN_CYCLE_EMPTY,
  SCAN_CYCLE_HEADERS,
  SCAN_CYCLE_TITLE,
  SCAN_CYCLE_UNAVAILABLE,
  scanCycleBadgeLabel,
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

  return (
    <details className="scan-cycle-history-panel discovery-disclosure">
      <summary className="discovery-summary">
        <span className="discovery-chevron" aria-hidden="true" />
        <span className="audit-summary-copy">
          {SCAN_CYCLE_TITLE} · {latest}
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

        {available && rows.length > 0 ? (
          <div className="table-wrap">
            <table className="scan-cycle-table">
              <thead>
                <tr>
                  {SCAN_CYCLE_HEADERS.map((header) => (
                    <th key={header}>{header}</th>
                  ))}
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
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </details>
  );
}
