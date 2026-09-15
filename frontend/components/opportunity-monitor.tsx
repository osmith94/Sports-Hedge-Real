"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import {
  buildMappingReviewPrompt,
  confirmMappingReview,
  interpretMappingReview,
  LiveRefreshStatus,
  NearOpportunity,
} from "../lib/api";
import { MappingVerificationPanel } from "./mapping-verification-panel";
import {
  MappingProposedRule,
  MappingVerdict,
  mappingConfirmRequest,
  structuralActivationBlockedReason,
} from "../lib/mapping-verification";
import {
  OBSERVATION_AGE_TICK_MS,
  formatObservationAge,
  observationTimestampTitle,
  startSharedObservationAgeTimer,
} from "../lib/observation-age";
import {
  OPPORTUNITY_MONITOR_SORT_COLUMNS,
  OPPORTUNITY_MONITOR_SORT_LABELS,
  OpportunityMonitorRow,
  OpportunityMonitorSortColumn,
  OpportunityMonitorSortState,
  ariaSortForOpportunityColumn,
  formatMonitorMoney,
  formatMonitorPercent,
  formatOpportunityLegLine,
  newestObservationAgeLabel,
  nextOpportunityMonitorSort,
  opportunityMonitorRows,
  opportunityMonitorStateTone,
  opportunityMonitorSummary,
  opportunitySortIndicator,
  sortOpportunityMonitor,
} from "../lib/opportunity-monitor-display";
import { shouldNavigateFromRowClick } from "../lib/tracked-markets-display";

export function OpportunityMonitor({
  items,
  available,
  liveRefresh,
  liveRefreshAvailable,
}: {
  items: NearOpportunity[];
  available: boolean;
  liveRefresh: LiveRefreshStatus | null;
  liveRefreshAvailable: boolean;
}) {
  const router = useRouter();
  const [sort, setSort] = useState<OpportunityMonitorSortState | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const [verifyId, setVerifyId] = useState<string | null>(null);
  const [promptById, setPromptById] = useState<Record<string, string>>({});
  const [proposedById, setProposedById] = useState<Record<string, MappingProposedRule | null>>({});
  const [blockedById, setBlockedById] = useState<Record<string, string | null>>({});
  const [verifyNoteById, setVerifyNoteById] = useState<Record<string, string | null>>({});
  const reviewIdById = useRef<Record<string, string>>({});
  const rows = useMemo(() => opportunityMonitorRows(items), [items]);
  const sortedRows = useMemo(() => sortOpportunityMonitor(rows, sort), [rows, sort]);
  const summary = useMemo(
    () => opportunityMonitorSummary(rows, liveRefresh, available, liveRefreshAvailable, nowMs),
    [rows, liveRefresh, available, liveRefreshAvailable, nowMs],
  );

  useEffect(() => {
    setNowMs(Date.now());
    return startSharedObservationAgeTimer(setNowMs, { tickMs: OBSERVATION_AGE_TICK_MS });
  }, []);

  function toggleExpanded(id: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function openVerify(row: OpportunityMonitorRow) {
    if (!row.offerVerify || !row.mappingCandidate) return;
    setExpanded((current) => new Set(current).add(row.id));
    setVerifyId(row.id);
    setVerifyNoteById((current) => ({ ...current, [row.id]: null }));
    if (promptById[row.id]) return;
    try {
      const bundle = await buildMappingReviewPrompt(row.mappingCandidate);
      setPromptById((current) => ({ ...current, [row.id]: bundle.prompt_text }));
    } catch (error) {
      setVerifyNoteById((current) => ({
        ...current,
        [row.id]: error instanceof Error ? error.message : "mapping prompt unavailable",
      }));
    }
  }

  async function interpretVerify(
    row: OpportunityMonitorRow,
    input: { chatgptText: string; verdict: MappingVerdict },
  ) {
    if (!row.mappingCandidate) return;
    try {
      const proposal = await interpretMappingReview({
        candidate: row.mappingCandidate,
        chatgpt_text: input.chatgptText,
        manual_verdict: input.verdict,
      });
      reviewIdById.current[row.id] = proposal.review_id;
      const structuralBlock = structuralActivationBlockedReason(proposal.activation_blocked_reason);
      setProposedById((current) => ({ ...current, [row.id]: proposal.proposed_rule }));
      setBlockedById((current) => ({ ...current, [row.id]: structuralBlock }));
      setVerifyNoteById((current) => ({
        ...current,
        [row.id]: structuralBlock
          ? `Interpret only · ${structuralBlock}`
          : "Interpreted without saving. Explicit confirmation is still required.",
      }));
    } catch (error) {
      setVerifyNoteById((current) => ({
        ...current,
        [row.id]: error instanceof Error ? error.message : "interpret failed",
      }));
    }
  }

  async function confirmVerify(
    row: OpportunityMonitorRow,
    input: { chatgptText: string; verdict: MappingVerdict },
  ) {
    if (!row.mappingCandidate) return;
    try {
      const proposal = await confirmMappingReview(
        mappingConfirmRequest({
          candidate: row.mappingCandidate,
          chatgptText: input.chatgptText,
          verdict: input.verdict,
          operatorConfirmed: true,
          reviewId: reviewIdById.current[row.id],
        }),
      );
      setProposedById((current) => ({ ...current, [row.id]: proposal.proposed_rule }));
      setBlockedById((current) => ({ ...current, [row.id]: proposal.activation_blocked_reason ?? null }));
      setVerifyNoteById((current) => ({
        ...current,
        [row.id]:
          "Learned rule saved. operator_verified provenance appears after a later scan applies it; this row stays current-scan mapping.",
      }));
    } catch (error) {
      setVerifyNoteById((current) => ({
        ...current,
        [row.id]: error instanceof Error ? error.message : "confirm failed",
      }));
    }
  }

  return (
    <section className="panel opportunity-monitor">
      <div className="panel-header">
        <div>
          <div className="panel-title">Opportunity Monitor</div>
          <div className="panel-meta">
            Current and recent cross-venue opportunities from Fast Scan + Full Sweep.
            Paper describes execution mode, not this table. Current radar only — not the
            append-only scan audit.
          </div>
        </div>
        <span className={available ? "status-badge" : "demo-chip"}>
          {available ? (rows.length ? "LIVE PAPER" : "EMPTY") : "UNAVAILABLE"}
        </span>
      </div>

      <OpportunitySummaryStrip summary={summary} nowMs={nowMs} available={available} />

      {!available ? (
        <div className="empty-live-compact">
          Current opportunity radar unavailable. No fabricated opportunities.
        </div>
      ) : null}
      {available && rows.length === 0 ? (
        <div className="empty-live-compact">
          No current cross-venue opportunities. Empty current radar is not back-filled from
          audit history or demo fixtures.
        </div>
      ) : null}

      {available && rows.length > 0 ? (
        <div className="table-wrap">
          <table>
            <caption className="scan-history-caption">
              Current radar set from tracked watchlist / FixtureCurrentStateStore. Age uses each
              row&apos;s last_scanned_at (else last_seen_at), not browser receipt time. Default
              order is qualifying, then near, then other current states, then highest net edge,
              then recency. User sorting applies to this loaded current set only.
            </caption>
            <thead>
              <tr>
                <th scope="col" className="opportunity-expand-col">
                  <span className="visually-hidden">Expand legs</span>
                </th>
                {OPPORTUNITY_MONITOR_SORT_COLUMNS.map((column) => (
                  <SortableHeader
                    column={column}
                    key={column}
                    onToggle={() => setSort((current) => nextOpportunityMonitorSort(current, column))}
                    sort={sort}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {sortedRows.map((row) => {
                const open = expanded.has(row.id);
                return (
                  <Fragment key={row.id}>
                    <MonitorRow
                      nowMs={nowMs}
                      onExpand={() => toggleExpanded(row.id)}
                      onNavigate={(href) => router.push(href)}
                      onVerify={() => void openVerify(row)}
                      open={open}
                      row={row}
                    />
                    {open ? (
                      <tr className="opportunity-detail-row">
                        <td colSpan={OPPORTUNITY_MONITOR_SORT_COLUMNS.length + 1}>
                          <OpportunityRowDetail
                            blockedReason={blockedById[row.id] ?? null}
                            note={verifyNoteById[row.id] ?? null}
                            onConfirm={(input) => void confirmVerify(row, input)}
                            onCopyPrompt={(prompt) => {
                              void navigator.clipboard?.writeText(prompt);
                            }}
                            onInterpret={(input) => void interpretVerify(row, input)}
                            promptText={promptById[row.id] ?? ""}
                            proposedRule={proposedById[row.id] ?? null}
                            row={row}
                            verifying={verifyId === row.id}
                          />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

function OpportunitySummaryStrip({
  summary,
  nowMs,
  available,
}: {
  summary: ReturnType<typeof opportunityMonitorSummary>;
  nowMs: number;
  available: boolean;
}) {
  const cells = [
    {
      label: "Qualifying",
      value: available ? String(summary.qualifyingCount ?? 0) : "—",
    },
    {
      label: "Near arb",
      value: available ? String(summary.nearCount ?? 0) : "—",
    },
    {
      label: "Best net edge",
      value: formatMonitorPercent(summary.bestNetEdge),
    },
    {
      label: "Newest observation",
      value: newestObservationAgeLabel(summary, nowMs),
      title: summary.newestObservedAt
        ? observationTimestampTitle(summary.newestObservedAt)
        : "no current observation",
    },
    {
      label: "Active venues",
      value: summary.activeVenues,
    },
    {
      label: "Fast Scan",
      value: summary.fastScan,
    },
    {
      label: "Full Sweep",
      value: summary.fullSweep,
    },
  ];
  return (
    <div className="opportunity-summary" aria-label="Current opportunity summary">
      {cells.map((cell) => (
        <div className="opportunity-summary-cell" key={cell.label} title={cell.title}>
          <div className="metric-label">{cell.label}</div>
          <div className="opportunity-summary-value">{cell.value}</div>
        </div>
      ))}
    </div>
  );
}

function MonitorRow({
  row,
  open,
  nowMs,
  onExpand,
  onNavigate,
  onVerify,
}: {
  row: OpportunityMonitorRow;
  open: boolean;
  nowMs: number;
  onExpand: () => void;
  onNavigate: (href: string) => void;
  onVerify: () => void;
}) {
  const tone = opportunityMonitorStateTone(row.state);
  return (
    <tr
      className={row.href ? "tracked-row is-navigable" : undefined}
      onClick={(event) => {
        if (
          !row.href ||
          !shouldNavigateFromRowClick({
            button: event.button,
            metaKey: event.metaKey,
            ctrlKey: event.ctrlKey,
            altKey: event.altKey,
            shiftKey: event.shiftKey,
            defaultPrevented: event.defaultPrevented,
            selectedText:
              typeof window !== "undefined" ? window.getSelection()?.toString() ?? "" : "",
            target: event.target as { closest?: (selector: string) => unknown },
          })
        ) {
          return;
        }
        onNavigate(row.href);
      }}
    >
      <td>
        <button
          aria-expanded={open}
          aria-label={`Toggle outcome legs for ${row.eventLabel}`}
          className="opportunity-expand"
          onClick={(event) => {
            event.stopPropagation();
            onExpand();
          }}
          type="button"
        >
          {open ? "▾" : "▸"}
        </button>
      </td>
      <td>
        <time
          dateTime={row.observedAt ?? undefined}
          suppressHydrationWarning
          title={observationTimestampTitle(row.observedAt)}
        >
          {formatObservationAge(row.observedAt, nowMs)}
        </time>
      </td>
      <td className="row-title">
        {row.href ? (
          <Link
            className="fixture-link tracked-market-link"
            href={row.href}
            onClick={(event) => event.stopPropagation()}
            title="Open fixture inventory"
          >
            {row.eventLabel}
          </Link>
        ) : (
          row.eventLabel
        )}
      </td>
      <td>{row.marketLabel}</td>
      <td className="muted">{row.venuesLabel}</td>
      <td>{formatMonitorPercent(row.grossEdge)}</td>
      <td className={row.netEdge !== null && row.netEdge < 0 ? "edge-negative" : row.netEdge !== null ? "edge" : ""}>
        {formatMonitorPercent(row.netEdge)}
      </td>
      <td>{formatMonitorMoney(row.executableSizeGbp)}</td>
      <td className={row.guaranteedProfitGbp !== null ? "edge" : ""}>
        {formatMonitorMoney(row.guaranteedProfitGbp)}
      </td>
      <td>{row.riskScore ?? "—"}</td>
      <td className="opportunity-mapping" title={row.mappingTitle}>
        <div>{row.mappingText}</div>
        {row.offerVerify ? (
          <button
            className="opportunity-mapping-verify"
            onClick={(event) => {
              event.stopPropagation();
              onVerify();
            }}
            type="button"
          >
            Verify
          </button>
        ) : null}
      </td>
      <td>
        <span className={`ops-status is-${tone}`} title={row.stateTitle}>
          {row.state}
        </span>
      </td>
      <td className="muted">
        {row.laneLabel}
        <div>{row.freshnessLabel}</div>
      </td>
    </tr>
  );
}

function OpportunityRowDetail({
  row,
  verifying,
  promptText,
  proposedRule,
  blockedReason,
  note,
  onCopyPrompt,
  onInterpret,
  onConfirm,
}: {
  row: OpportunityMonitorRow;
  verifying: boolean;
  promptText: string;
  proposedRule: MappingProposedRule | null;
  blockedReason: string | null;
  note: string | null;
  onCopyPrompt: (prompt: string) => void;
  onInterpret: (input: { chatgptText: string; verdict: MappingVerdict }) => void;
  onConfirm: (input: { chatgptText: string; verdict: MappingVerdict }) => void;
}) {
  return (
    <div className="opportunity-detail">
      <p className="panel-meta">{row.rejectionDetail}</p>
      <p className="panel-meta">
        Exact observation {observationTimestampTitle(row.observedAt)} · quote {row.quoteAgeLabel}
      </p>
      <p className="panel-meta" title={row.mappingTitle}>
        Mapping {row.mappingText}
        {row.offerVerify ? " · Verify available for current evidence" : ""}
      </p>
      {row.legs.length === 0 ? (
        <div className="empty-live-compact">
          No outcome legs on this radar row. Per-leg action/freshness are unknown unless present
          on the watchlist read model.
        </div>
      ) : (
        <ul className="opportunity-legs">
          {row.legs.map((leg, index) => (
            <li key={`${row.id}-${leg.venue}-${leg.outcome}-${index}`}>
              {formatOpportunityLegLine(leg)}
              {leg.sourceRunnerId !== "—" ? ` · runner ${leg.sourceRunnerId}` : ""}
            </li>
          ))}
        </ul>
      )}
      {verifying && row.mappingCandidate ? (
        <MappingVerificationPanel
          activationBlockedReason={blockedReason}
          candidate={row.mappingCandidate}
          onConfirm={onConfirm}
          onCopyPrompt={onCopyPrompt}
          onInterpret={onInterpret}
          promptText={promptText}
          proposedRule={proposedRule}
          provenance={row.mappingProvenance}
        />
      ) : null}
      {note ? <p className="panel-meta">{note}</p> : null}
    </div>
  );
}

function SortableHeader({
  column,
  sort,
  onToggle,
}: {
  column: OpportunityMonitorSortColumn;
  sort: OpportunityMonitorSortState | null;
  onToggle: () => void;
}) {
  const label = OPPORTUNITY_MONITOR_SORT_LABELS[column];
  const indicator = opportunitySortIndicator(column, sort);
  const ariaSort = ariaSortForOpportunityColumn(column, sort);
  const directionLabel = ariaSort === "none" ? "" : `, currently ${ariaSort}`;
  const ageHint = column === "age" ? ", from last_scanned_at or last_seen_at" : "";
  const loadedHint = ", loaded current set only";
  return (
    <th aria-sort={ariaSort} className="sortable" scope="col">
      <button
        aria-label={`Sort by ${label}${ageHint}${loadedHint}${directionLabel}`}
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
