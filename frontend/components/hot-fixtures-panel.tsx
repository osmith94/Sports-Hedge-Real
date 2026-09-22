"use client";

import Link from "next/link";

import { LiveRefreshStatus } from "../lib/api";
import {
  DEFERRED_CROSS_VENUE_HEADING,
  DEFERRED_NOT_HOT_CAPACITY,
  HOT_PRICING_HEADING,
  HOT_ROSTER_COPY,
  HOT_ROSTER_EMPTY,
  HOT_ROSTER_HEADERS,
  HOT_ROSTER_TITLE,
  HOT_ROSTER_UNAVAILABLE,
  HOT_ZONE_KICKER,
  POST_KICKOFF_PENDING_HEADING,
  deferredFixtureRows,
  deferredRosterSummary,
  fastScanRosterSummary,
  hotFixtureRows,
  hotPricingCount,
  hotRosterBadgeLabel,
  postKickoffPendingRows,
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
  const deferred = available ? deferredFixtureRows(status, nowMs) : [];
  const pending = available ? postKickoffPendingRows(status, nowMs) : [];
  const badge = hotRosterBadgeLabel(available, status);
  const summary = available && status ? fastScanRosterSummary(status) : null;
  const deferredSummary = available && status ? deferredRosterSummary(status) : null;
  const pricingCount = available && status ? hotPricingCount(status) : 0;

  return (
    <section className="panel hot-fixtures-panel hot-zone-panel">
      <div className="panel-header">
        <div>
          <div className="hot-zone-kicker">{HOT_ZONE_KICKER}</div>
          <div className="panel-title">{HOT_PRICING_HEADING}</div>
          <div className="panel-meta">{HOT_ROSTER_TITLE}. {HOT_ROSTER_COPY}</div>
          {summary ? <div className="hot-fast-scan-summary">{summary}</div> : null}
        </div>
        <span className={!available ? "demo-chip" : pricingCount > 0 ? "status-badge" : "status-badge status-badge-stopped"}>{badge}</span>
      </div>

      {!available || !status ? (
        <div className="empty-live-compact">{HOT_ROSTER_UNAVAILABLE}</div>
      ) : null}
      {available && status && rows.length === 0 ? (
        <div className="empty-live-compact">{HOT_ROSTER_EMPTY}</div>
      ) : null}

      {available && rows.length > 0 ? <FixtureTable rows={rows} nowMs={nowMs} /> : null}

      {available && status ? (
        <div className="hot-deferred-block">
          <div className="panel-title">{DEFERRED_CROSS_VENUE_HEADING}</div>
          <div className="panel-meta">{deferredSummary ?? DEFERRED_NOT_HOT_CAPACITY}</div>
          {deferred.length === 0 ? (
            <div className="empty-live-compact">No fixtures awaiting a cross-venue candidate.</div>
          ) : (
            <FixtureTable rows={deferred} nowMs={nowMs} deferred />
          )}
        </div>
      ) : null}

      {available && pending.length > 0 ? (
        <div className="hot-deferred-block">
          <div className="panel-title">{POST_KICKOFF_PENDING_HEADING}</div>
          <div className="panel-meta">Post-kickoff status is pending. Not counted as HOT pricing.</div>
          <FixtureTable rows={pending} nowMs={nowMs} deferred />
        </div>
      ) : null}
    </section>
  );
}

function FixtureTable({
  rows,
  nowMs,
  deferred = false,
}: {
  rows: ReturnType<typeof hotFixtureRows>;
  nowMs: number | null;
  deferred?: boolean;
}) {
  return (
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
                {kickoffLocalLabel(row.kickoffUtc, nowMs)}
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
                      <span className={deferred ? "hot-reason-chip hot-reason-chip-deferred" : "hot-reason-chip"} key={reason}>
                        {reason}
                      </span>
                    ))}
                  </div>
                ) : (
                  <span className="muted">{deferred ? "NOT HOT PRICED" : "HOT"}</span>
                )}
              </td>
              <td className="wrap">
                <span className={row.hasQualifyingOpportunity ? undefined : "muted"}>
                  {row.evaluationLabel}
                </span>
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
  );
}
