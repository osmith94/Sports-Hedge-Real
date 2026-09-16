"use client";

import Link from "next/link";

import { LiveRefreshStatus } from "../lib/api";
import {
  HOT_ROSTER_COPY,
  HOT_ROSTER_EMPTY,
  HOT_ROSTER_HEADERS,
  HOT_ROSTER_TITLE,
  HOT_ROSTER_UNAVAILABLE,
  hotFixtureRows,
  hotRosterBadgeLabel,
} from "../lib/hot-fixture-roster-display";
import { kickoffLocalLabel } from "../lib/format";
import { useHydratedNowMs } from "./hydrated-relative-time";

export function HotFixturesPanel({
  status,
  available,
}: {
  status: LiveRefreshStatus | null;
  available: boolean;
}) {
  const nowMs = useHydratedNowMs();
  const rows = available ? hotFixtureRows(status, nowMs) : [];
  const badge = hotRosterBadgeLabel(available, status);

  return (
    <section className="panel hot-fixtures-panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">{HOT_ROSTER_TITLE}</div>
          <div className="panel-meta">{HOT_ROSTER_COPY}</div>
        </div>
        <span className={available ? "status-badge" : "demo-chip"}>{badge}</span>
      </div>

      {!available || !status ? (
        <div className="empty-live-compact">{HOT_ROSTER_UNAVAILABLE}</div>
      ) : null}
      {available && status && rows.length === 0 ? (
        <div className="empty-live-compact">{HOT_ROSTER_EMPTY}</div>
      ) : null}

      {available && rows.length > 0 ? (
        <div className="table-wrap">
          <table className="hot-fixtures-table">
            <thead>
              <tr>
                {HOT_ROSTER_HEADERS.map((header) => (
                  <th key={header}>{header}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="row-title wrap">
                    <Link className="fixture-link" href={row.href}>
                      {row.name}
                    </Link>
                    <div className="muted">{row.competition}</div>
                  </td>
                  <td className="wrap">
                    {kickoffLocalLabel(row.kickoffUtc)}
                    {row.kickoffLines.map((line) => (
                      <div className="muted" key={line}>
                        {line}
                      </div>
                    ))}
                  </td>
                  <td className="wrap">
                    {row.reasons.length ? (
                      <div className="hot-reason-row">
                        {row.reasons.map((reason) => (
                          <span className="hot-reason-chip" key={reason}>
                            {reason}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <span className="muted">HOT</span>
                    )}
                  </td>
                  <td>{row.venuesLabel}</td>
                  <td className="muted">{row.lastRefresh}</td>
                  <td>{row.equivalentLabel}</td>
                  <td className={row.netEdgeLabel === "—" ? "muted" : undefined}>
                    {row.netEdgeLabel}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
