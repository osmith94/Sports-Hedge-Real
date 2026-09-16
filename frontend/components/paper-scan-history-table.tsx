"use client";

import { useEffect, useMemo, useState } from "react";

import { PaperScanRecord } from "../lib/api";
import { money, percent } from "../lib/format";
import { marketLabel } from "../lib/arbitrage-ops";
import {
  AUDIT_AGE_TICK_MS,
  DEFAULT_PAPER_SCAN_HISTORY_SORT,
  PAPER_SCAN_HISTORY_SORT_COLUMNS,
  PAPER_SCAN_HISTORY_SORT_LABELS,
  PaperScanHistorySortColumn,
  PaperScanHistorySortState,
  ariaSortForPaperScanColumn,
  auditScanTimestampTitle,
  formatAuditScanAge,
  nextPaperScanHistorySort,
  paperScanEventLabel,
  paperScanHistoryStatusText,
  paperScanSortIndicator,
  sortPaperScanHistory,
  startSharedAuditAgeTimer,
} from "../lib/paper-scan-history-display";

export function PaperScanHistoryTable({ scans }: { scans: PaperScanRecord[] }) {
  const [sort, setSort] = useState<PaperScanHistorySortState>(DEFAULT_PAPER_SCAN_HISTORY_SORT);
  const [nowMs, setNowMs] = useState<number | null>(null);
  const sortedScans = useMemo(() => sortPaperScanHistory(scans, sort), [scans, sort]);

  useEffect(() => {
    setNowMs(Date.now());
    // One shared timer for every visible age label. Do not add per-row intervals.
    return startSharedAuditAgeTimer(setNowMs, { tickMs: AUDIT_AGE_TICK_MS });
  }, []);

  return (
    <div className="table-wrap">
      <table>
        <caption className="scan-history-caption">
          Historical audit window. Age uses each row&apos;s <code>scanned_at</code>, not browser
          receipt time. Sorting applies to these loaded rows only.
        </caption>
        <thead>
          <tr>
            {PAPER_SCAN_HISTORY_SORT_COLUMNS.map((column) => (
              <SortableHeader
                column={column}
                key={column}
                onToggle={() => setSort((current) => nextPaperScanHistorySort(current, column))}
                sort={sort}
              />
            ))}
          </tr>
        </thead>
        <tbody>
          {sortedScans.map((item) => {
            const net = item.net_edge == null ? null : Number(item.net_edge);
            return (
              <tr key={item.record_id}>
                <td>
                  <time
                    dateTime={item.scanned_at}
                    suppressHydrationWarning
                    title={auditScanTimestampTitle(item.scanned_at)}
                  >
                    {formatAuditScanAge(item.scanned_at, nowMs)}
                  </time>
                </td>
                <td className="row-title">{paperScanEventLabel(item)}</td>
                <td>{marketLabel(item)}</td>
                <td className="muted">{item.venues.join(" / ")}</td>
                <td>{percent(item.gross_edge)}</td>
                <td className={net !== null && net < 0 ? "edge-negative" : net !== null ? "edge" : ""}>
                  {percent(item.net_edge)}
                  {net !== null && net < 0 ? " · below break-even" : ""}
                </td>
                <td>{money(item.executable_stake_gbp)}</td>
                <td
                  className={
                    item.guaranteed_profit_gbp !== null && item.guaranteed_profit_gbp !== undefined
                      ? "edge"
                      : ""
                  }
                >
                  {money(item.guaranteed_profit_gbp)}
                </td>
                <td className={item.execution_risk_band === "low" ? "risk-low" : "risk-medium"}>
                  {item.execution_risk_score ?? "—"}
                  {item.execution_risk_band ? ` · ${item.execution_risk_band}` : ""}
                </td>
                <td>{(item.mapping_confidence * 100).toFixed(1)}%</td>
                <td>{paperScanHistoryStatusText(item)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SortableHeader({
  column,
  sort,
  onToggle,
}: {
  column: PaperScanHistorySortColumn;
  sort: PaperScanHistorySortState | null;
  onToggle: () => void;
}) {
  const label = PAPER_SCAN_HISTORY_SORT_LABELS[column];
  const indicator = paperScanSortIndicator(column, sort);
  const ariaSort = ariaSortForPaperScanColumn(column, sort);
  const directionLabel = ariaSort === "none" ? "" : `, currently ${ariaSort}`;
  const ageHint = column === "age" ? ", from scanned_at" : "";
  return (
    <th aria-sort={ariaSort} className="sortable" scope="col">
      <button
        aria-label={`Sort by ${label}${ageHint}${directionLabel}`}
        className="sort-header"
        onClick={onToggle}
        type="button"
      >
        {label}
        {indicator ? (
          <span aria-hidden="true" className="sort-indicator">
            {indicator}
          </span>
        ) : null}
      </button>
    </th>
  );
}
