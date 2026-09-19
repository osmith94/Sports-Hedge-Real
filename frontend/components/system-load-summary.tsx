"use client";

import { LiveRefreshStatus } from "../lib/api";
import { systemLoadLines } from "../lib/system-load-display";

export function SystemLoadSummaryCard({
  status,
}: {
  status: LiveRefreshStatus | null;
}) {
  const lines = systemLoadLines(status);
  return (
    <div
      className="system-load"
      aria-label="System load"
      title="Current in-memory scanner load. Numbers only — not a safe/unsafe score, not market quotes."
    >
      <div className="system-load-title">SYSTEM LOAD</div>
      {lines.map((line) => (
        <div className="system-load-line" key={line.key}>
          <span className="system-load-key">{line.key}</span>
          {line.detail}
        </div>
      ))}
    </div>
  );
}
