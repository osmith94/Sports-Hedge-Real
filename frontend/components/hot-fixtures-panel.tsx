"use client";

import Link from "next/link";
import { useState } from "react";

import { DeferredFixtureReport, LiveRefreshStatus, getDeferredFixtureReport } from "../lib/api";
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
  deferredAwaitingCount,
  deferredFixtureRows,
  fastScanRosterSummary,
  hotFixtureRow,
  hotFixtureRows,
  hotPricingCount,
  hotRosterBadgeLabel,
  postKickoffPendingRows,
} from "../lib/hot-fixture-roster-display";
import { useLiveStatusOptional } from "./live-status-provider";
import { kickoffLocalLabel } from "../lib/format";
import { useHydratedNowMs } from "./hydrated-relative-time";

export const DEFERRED_ROSTER_REGION_ID = "hot-deferred-roster";

export function HotFixturesPanel({
  status: statusFromServer,
  available: availableFromServer,
}: {
  status?: LiveRefreshStatus | null;
  available?: boolean;
}) {
  const live = useLiveStatusOptional();
  const status = live?.status ?? statusFromServer ?? null;
  const available = live ? live.status != null || !live.settled : Boolean(availableFromServer);
  const nowMs = useHydratedNowMs();
  const rows = available ? hotFixtureRows(status, nowMs) : [];
  const deferred = available ? deferredFixtureRows(status, nowMs) : [];
  const pending = available ? postKickoffPendingRows(status, nowMs) : [];
  const badge = hotRosterBadgeLabel(available, status);
  const summary = available && status ? fastScanRosterSummary(status) : null;
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
        <DeferredCrossVenueSection status={status} rows={deferred} nowMs={nowMs} />
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

export function DeferredCrossVenueRoster({
  rows,
  nowMs,
  expanded,
  onToggle,
}: {
  rows: ReturnType<typeof hotFixtureRows>;
  nowMs: number | null;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="hot-deferred-block">
      <button
        type="button"
        className="hot-deferred-toggle"
        aria-expanded={expanded}
        aria-controls={DEFERRED_ROSTER_REGION_ID}
        onClick={onToggle}
      >
        <span className="panel-title">{DEFERRED_CROSS_VENUE_HEADING} · {rows.length}</span>
        <span className="panel-meta">
          {" "}· {DEFERRED_NOT_HOT_CAPACITY} {expanded ? "Collapse." : "Expand to inspect."}
        </span>
      </button>
      {expanded ? (
        <div id={DEFERRED_ROSTER_REGION_ID}>
          {rows.length === 0 ? (
            <div className="empty-live-compact">No fixtures awaiting a cross-venue candidate.</div>
          ) : (
            <FixtureTable rows={rows} nowMs={nowMs} deferred />
          )}
        </div>
      ) : null}
    </div>
  );
}

function DeferredCrossVenueSection({
  status,
  rows,
  nowMs,
}: {
  status: LiveRefreshStatus;
  rows: ReturnType<typeof hotFixtureRows>;
  nowMs: number | null;
}) {
  const counted = typeof status.deferred_awaiting_count === "number";
  const [expanded, setExpanded] = useState(false);
  const [report, setReport] = useState<DeferredFixtureReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadedRows = report
    ? report.rows.map((row) =>
        hotFixtureRow(
          {
            source: "matchbook",
            source_event_id: row.canonical_event_id,
            canonical_event_id: row.canonical_event_id,
            home_team: row.home_team,
            away_team: row.away_team,
            competition: row.competition,
            kickoff_utc: row.kickoff_utc,
            polymarket_matched: Boolean(row.polymarket_matched),
            matchbook_matched: row.matchbook_matched,
            kalshi_matched: row.kalshi_matched,
            live_score_supported: false,
            last_seen_at: row.last_seen_at || row.kickoff_utc,
            matched_market_count: 0,
            solver_is_arbitrage: false,
            market_evaluation_state: row.market_evaluation_state,
            market_evaluation_reason: row.market_evaluation_reason,
            hot_reasons: row.hot_reasons,
            scan_lane: row.scan_lane,
            in_running: row.in_running,
            fixture_status: row.fixture_status,
            sport: row.sport,
            target_competition_code: row.target_competition_code,
          },
          nowMs,
        ),
      )
    : rows;
  if (!counted) {
    return (
      <DeferredCrossVenueRoster
        rows={rows}
        nowMs={nowMs}
        expanded={expanded}
        onToggle={() => setExpanded((current) => !current)}
      />
    );
  }
  const count = deferredAwaitingCount(status);
  return (
    <div className="hot-deferred-block">
      <button
        type="button"
        className="hot-deferred-toggle"
        aria-expanded={expanded}
        aria-controls={DEFERRED_ROSTER_REGION_ID}
        onClick={() => {
          const next = !expanded;
          setExpanded(next);
          if (!next || report || loading) return;
          setLoading(true);
          setError(null);
          void getDeferredFixtureReport()
            .then((nextReport) => setReport(nextReport))
            .catch(() => setError("Deferred fixture report unavailable. Nothing was fabricated."))
            .finally(() => setLoading(false));
        }}
      >
        <span className="panel-title">{DEFERRED_CROSS_VENUE_HEADING} · {count}</span>
        <span className="panel-meta">
          {" "}· {DEFERRED_NOT_HOT_CAPACITY} {expanded ? "Collapse." : "Inspect deferred fixtures."}
        </span>
      </button>
      {expanded ? (
        <div id={DEFERRED_ROSTER_REGION_ID}>
          {loading ? <div className="empty-live-compact">Loading deferred fixture diagnostic.</div> : null}
          {error ? <div className="empty-live-compact">{error}</div> : null}
          {!loading && !error && loadedRows.length === 0 ? (
            <div className="empty-live-compact">No fixtures awaiting a cross-venue candidate.</div>
          ) : null}
          {!loading && !error && loadedRows.length > 0 ? (
            <FixtureTable rows={loadedRows} nowMs={nowMs} deferred />
          ) : null}
        </div>
      ) : null}
    </div>
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
