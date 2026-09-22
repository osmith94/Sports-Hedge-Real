"use client";

import { Fragment } from "react";

import { LiveRefreshStatus } from "../lib/api";
import { systemLoadDetailLines, systemLoadLines } from "../lib/system-load-display";

export function SystemLoadSummaryCard({
  status,
}: {
  status: LiveRefreshStatus | null;
}) {
  const lines = systemLoadLines(status?.system_load);
  const details = systemLoadDetailLines(status?.system_load);
  return (
    <div
      className="system-load"
      aria-label="System load"
      title="Current in-memory scanner load from backend system_load. Numbers only — not a safe/unsafe score, not market quotes."
    >
      <div className="system-load-title">SYSTEM LOAD</div>
      {lines.map((line) => (
        <Fragment key={`${line.key}-${line.detail}`}>
          <span className="system-load-key">{line.key}</span>
          <span className="system-load-detail" title={line.detail}>
            {line.detail}
          </span>
        </Fragment>
      ))}
      <details className="system-load-diagnostics">
        <summary>Diagnostics</summary>
        <div className="system-load-detail-grid">
          {details.map((line) => (
            <Fragment key={`detail-${line.key}-${line.detail}`}>
              <span className="system-load-key">{line.key}</span>
              <span className="system-load-detail" title={line.detail}>
                {line.detail}
              </span>
            </Fragment>
          ))}
        </div>
      </details>
    </div>
  );
}
