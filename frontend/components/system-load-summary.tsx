"use client";

import { LiveRefreshStatus } from "../lib/api";
import { systemLoadLines } from "../lib/system-load-display";

export function SystemLoadSummaryCard({
  status,
}: {
  status: LiveRefreshStatus | null;
}) {
  const lines = systemLoadLines(status?.system_load);
  return (
    <div
      className="system-load"
      aria-label="System load"
      title="Current in-memory scanner load from backend system_load. Numbers only — not a safe/unsafe score, not market quotes."
    >
      <div className="system-load-title">SYSTEM LOAD</div>
      {lines.map((line) => (
        <div className="system-load-line" key={`${line.key}-${line.detail}`}>
          {line.key ? <span className="system-load-key">{line.key}</span> : null}
          {line.detail}
        </div>
      ))}
    </div>
  );
}
