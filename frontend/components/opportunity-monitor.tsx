"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Fragment, useEffect, useMemo, useState } from "react";

import {
  buildMappingReviewPrompt,
  confirmMappingReview,
  getTrackedWatchlist,
  interpretMappingReview,
  LiveRefreshStatus,
  NearOpportunity,
  OPPORTUNITY_MONITOR_DEFAULT_LIMIT,
  OPPORTUNITY_MONITOR_MAX_LIMIT,
  opportunityMonitorTrackedQuery,
} from "../lib/api";
import { useLiveStatusOptional } from "./live-status-provider";
import { MappingVerificationPanel } from "./mapping-verification-panel";
import {
  MappingProposedRule,
  MappingReviewCandidate,
  MappingVerdict,
  fingerprintOpportunityVerifyRow,
  mappingVerifyConfirmDecision,
  snapshotMappingCandidate,
  structuralActivationBlockedReason,
  verifySessionMatchesCurrent,
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

type MappingVerifySession = {
  fingerprint: string;
  candidate: MappingReviewCandidate;
  prompt: string;
  reviewId?: string;
  proposed: MappingProposedRule | null;
  blocked: string | null;
};

const EVIDENCE_CHANGED_NOTE =
  "Current mapping evidence changed. Start a fresh Verify. Previous interpretation was not confirmed.";

export function OpportunityMonitor({
  items,
  available,
  liveRefresh: liveRefreshProp = null,
  liveRefreshAvailable: liveRefreshAvailableProp = false,
}: {
  items: NearOpportunity[];
  available: boolean;
  liveRefresh?: LiveRefreshStatus | null;
  liveRefreshAvailable?: boolean;
}) {
  const router = useRouter();
  const live = useLiveStatusOptional();
  const liveRefresh = live?.status ?? liveRefreshProp;
  const liveRefreshAvailable = live ? live.status != null : liveRefreshAvailableProp;
  const [trackedItems, setTrackedItems] = useState(items);
  const [trackedAvailable, setTrackedAvailable] = useState(available);
  const [sort, setSort] = useState<OpportunityMonitorSortState | null>(null);
  const [displayLimit, setDisplayLimit] = useState(OPPORTUNITY_MONITOR_DEFAULT_LIMIT);
  const [nowMs, setNowMs] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const [verifyId, setVerifyId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<Record<string, MappingVerifySession>>({});
  const [verifyNoteById, setVerifyNoteById] = useState<Record<string, string | null>>({});
  const rows = useMemo(() => opportunityMonitorRows(trackedItems), [trackedItems]);
  const sortedRows = useMemo(() => sortOpportunityMonitor(rows, sort), [rows, sort]);
  const summary = useMemo(
    () => opportunityMonitorSummary(rows, liveRefresh, trackedAvailable, liveRefreshAvailable, nowMs),
    [rows, liveRefresh, trackedAvailable, liveRefreshAvailable, nowMs],
  );

  useEffect(() => {
    setTrackedItems(items);
    setTrackedAvailable(available);
  }, [items, available]);

  const cycleStamp = live?.cycleStamp;
  useEffect(() => {
    if (cycleStamp == null && displayLimit === OPPORTUNITY_MONITOR_DEFAULT_LIMIT) return undefined;
    let cancelled = false;
    getTrackedWatchlist(opportunityMonitorTrackedQuery(displayLimit))
      .then((next) => {
        if (cancelled) return;
        setTrackedItems(next);
        setTrackedAvailable(true);
      })
      .catch(() => {
        // Keep the last loaded watchlist. A failed refresh is not an empty radar.
      });
    return () => {
      cancelled = true;
    };
  }, [cycleStamp, displayLimit]);

  useEffect(() => {
    setNowMs(Date.now());
    return startSharedObservationAgeTimer(setNowMs, { tickMs: OBSERVATION_AGE_TICK_MS });
  }, []);

  useEffect(() => {
    const fingerprintById = new Map(
      rows.map((row) => [row.id, fingerprintOpportunityVerifyRow(row)]),
    );
    const staleIds = Object.entries(sessions)
      .filter(([id, session]) => fingerprintById.get(id) !== session.fingerprint)
      .map(([id]) => id);
    if (staleIds.length === 0) return;
    setSessions((existing) => {
      const next = { ...existing };
      for (const id of staleIds) {
        delete next[id];
      }
      return next;
    });
    setVerifyNoteById((notes) => {
      const next = { ...notes };
      for (const id of staleIds) {
        next[id] = EVIDENCE_CHANGED_NOTE;
      }
      return next;
    });
    setVerifyId((id) => (id && staleIds.includes(id) ? null : id));
  }, [rows, sessions]);

  function failClosedEvidenceChanged(id: string) {
    setSessions((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
    setVerifyId((current) => (current === id ? null : current));
    setVerifyNoteById((current) => ({ ...current, [id]: EVIDENCE_CHANGED_NOTE }));
  }

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
    const fingerprint = fingerprintOpportunityVerifyRow(row);
    if (!fingerprint) return;
    setExpanded((current) => new Set(current).add(row.id));
    setVerifyId(row.id);
    const existing = sessions[row.id];
    if (existing && verifySessionMatchesCurrent(existing.fingerprint, fingerprint) && existing.prompt) {
      setVerifyNoteById((current) => ({ ...current, [row.id]: null }));
      return;
    }
    const snapshot = snapshotMappingCandidate(row.mappingCandidate);
    setSessions((current) => ({
      ...current,
      [row.id]: {
        fingerprint,
        candidate: snapshot,
        prompt: "",
        proposed: null,
        blocked: null,
      },
    }));
    setVerifyNoteById((current) => ({ ...current, [row.id]: null }));
    try {
      const bundle = await buildMappingReviewPrompt(snapshot);
      setSessions((current) => {
        const live = current[row.id];
        if (!live || live.fingerprint !== fingerprint) return current;
        return { ...current, [row.id]: { ...live, prompt: bundle.prompt_text } };
      });
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
    const fingerprint = fingerprintOpportunityVerifyRow(row);
    const session = sessions[row.id];
    if (
      !verifySessionMatchesCurrent(session?.fingerprint, fingerprint) ||
      !session?.candidate
    ) {
      failClosedEvidenceChanged(row.id);
      return;
    }
    try {
      const proposal = await interpretMappingReview({
        candidate: session.candidate,
        chatgpt_text: input.chatgptText,
        manual_verdict: input.verdict,
      });
      setSessions((current) => {
        const live = current[row.id];
        if (!live || live.fingerprint !== fingerprint) return current;
        return {
          ...current,
          [row.id]: {
            ...live,
            reviewId: proposal.review_id,
            proposed: proposal.proposed_rule,
            blocked: structuralActivationBlockedReason(proposal.activation_blocked_reason),
          },
        };
      });
      const structuralBlock = structuralActivationBlockedReason(proposal.activation_blocked_reason);
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
    const fingerprint = fingerprintOpportunityVerifyRow(row);
    const session = sessions[row.id];
    const decision = mappingVerifyConfirmDecision({
      sessionFingerprint: session?.fingerprint,
      currentFingerprint: fingerprint,
      sessionCandidate: session?.candidate,
      chatgptText: input.chatgptText,
      verdict: input.verdict,
      reviewId: session?.reviewId,
    });
    if (!decision.ok) {
      failClosedEvidenceChanged(row.id);
      return;
    }
    try {
      const proposal = await confirmMappingReview(decision.request);
      setSessions((current) => {
        const live = current[row.id];
        if (!live || live.fingerprint !== fingerprint) return current;
        return {
          ...current,
          [row.id]: {
            ...live,
            proposed: proposal.proposed_rule,
            blocked: structuralActivationBlockedReason(proposal.activation_blocked_reason),
          },
        };
      });
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
            Current and recent cross-venue opportunities from HOT pricing, BACKGROUND pricing and UNIVERSE discovery.
            Paper describes execution mode, not this table. Current radar only — not the
            append-only scan audit.
          </div>
        </div>
        <div className="panel-header-actions">
          <label className="panel-meta">
            Snapshot{" "}
            <select
              aria-label="Opportunity snapshot size"
              onChange={(event) => {
                const next = Number(event.target.value);
                setDisplayLimit(
                  next === OPPORTUNITY_MONITOR_MAX_LIMIT
                    ? OPPORTUNITY_MONITOR_MAX_LIMIT
                    : OPPORTUNITY_MONITOR_DEFAULT_LIMIT,
                );
              }}
              value={displayLimit}
            >
              <option value={OPPORTUNITY_MONITOR_DEFAULT_LIMIT}>20 recent</option>
              <option value={OPPORTUNITY_MONITOR_MAX_LIMIT}>50 recent</option>
            </select>
          </label>
          <span className={available ? "status-badge" : "demo-chip"}>
            {available ? (rows.length ? "LIVE PAPER" : "EMPTY") : "UNAVAILABLE"}
          </span>
        </div>
      </div>

      <OpportunitySummaryStrip summary={summary} nowMs={nowMs} available={available} />

      {!available ? (
        <div className="empty-live-compact">
          Current opportunity radar unavailable. No fabricated opportunities.
        </div>
      ) : null}
      {available && rows.length === 0 ? (
        <div className="empty-live-compact">
          No current opportunities in this {displayLimit}-row snapshot. An empty snapshot does not mean HOT, BACKGROUND, or UNIVERSE have stopped. Lane status above is the scanner heartbeat. Empty current radar is not back-filled from audit history or demo fixtures.
        </div>
      ) : null}

      {available && rows.length > 0 ? (
        <div className="table-wrap">
          <table>
            <caption className="scan-history-caption">
              Current radar set from tracked watchlist / FixtureCurrentStateStore. Snapshot of the{" "}
              {displayLimit} most recently observed current opportunities. The request is limit=
              {displayLimit}, not a longer list sliced in the browser. Price age is each row&apos;s
              last priced time. Last discovered/confirmed stays separate. Quote age is the provider
              quote at last evaluation. Radar retention is not executable quote freshness. Unknown
              price clocks stay unknown. State is economic/radar classification and does not replace
              the primary badge. Net edge stays on the row. Default order is most recently observed.
              Sorting a column reorders this loaded current set only. Rejected and single-venue rows
              stay labelled as such.
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
                const session = sessions[row.id];
                const currentFingerprint = fingerprintOpportunityVerifyRow(row);
                const sessionLive = Boolean(
                  session &&
                    verifySessionMatchesCurrent(session.fingerprint, currentFingerprint),
                );
                const liveSession = sessionLive ? session : undefined;
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
                            blockedReason={liveSession?.blocked ?? null}
                            note={verifyNoteById[row.id] ?? null}
                            onConfirm={(input) => void confirmVerify(row, input)}
                            onCopyPrompt={(prompt) => {
                              void navigator.clipboard?.writeText(prompt);
                            }}
                            onInterpret={(input) => void interpretVerify(row, input)}
                            promptText={liveSession?.prompt ?? ""}
                            proposedRule={liveSession?.proposed ?? null}
                            row={row}
                            sessionCandidate={liveSession?.candidate ?? null}
                            sessionKey={liveSession?.fingerprint ?? null}
                            verifying={verifyId === row.id && sessionLive}
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
  nowMs: number | null;
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
      label: "Newest price observation",
      value: newestObservationAgeLabel(summary, nowMs),
      title: summary.newestObservedAt
        ? observationTimestampTitle(summary.newestObservedAt)
        : "no current observation",
    },
    {
      label: "Last scan venues",
      value: summary.activeVenues,
    },
    {
      label: "HOT pricing",
      value: summary.fastScan,
    },
    {
      label: "BACKGROUND pricing",
      value: summary.backgroundPricing,
    },
    {
      label: "UNIVERSE discovery",
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
  nowMs: number | null;
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
          title={`Last priced ${observationTimestampTitle(row.observedAt, "unknown")} · Last discovered/confirmed ${observationTimestampTitle(row.discoveredAt, "unknown")} · quote ${row.quoteAgeLabel}`}
        >
          {formatObservationAge(row.observedAt, nowMs)}
        </time>
        <div className="muted" title={observationTimestampTitle(row.discoveredAt, "discovery unknown")}>
          discovered {formatObservationAge(row.discoveredAt, nowMs)}
        </div>
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
      <td className="muted" title="Radar retention is not executable quote freshness">
        {row.laneLabel}
        <div>{row.freshnessLabel}</div>
        <div>quote {row.quoteAgeLabel}</div>
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
  sessionCandidate,
  sessionKey,
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
  sessionCandidate: MappingReviewCandidate | null;
  sessionKey: string | null;
  onCopyPrompt: (prompt: string) => void;
  onInterpret: (input: { chatgptText: string; verdict: MappingVerdict }) => void;
  onConfirm: (input: { chatgptText: string; verdict: MappingVerdict }) => void;
}) {
  return (
    <div className="opportunity-detail">
      <p className="panel-meta">{row.rejectionDetail}</p>
      <p className="panel-meta">
        Last priced {observationTimestampTitle(row.observedAt, "unknown")} · Last discovered/confirmed{" "}
        {observationTimestampTitle(row.discoveredAt, "unknown")} · quote {row.quoteAgeLabel}
      </p>
      <p className="panel-meta">
        Radar retention {row.freshnessLabel}. This is not executable quote freshness.
      </p>
      {row.economicsNote ? <p className="panel-meta">{row.economicsNote}</p> : null}
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
      {verifying && sessionCandidate && sessionKey ? (
        <MappingVerificationPanel
          activationBlockedReason={blockedReason}
          candidate={sessionCandidate}
          key={sessionKey}
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
  const ageHint = column === "age" ? ", from last priced time; unknown stays unknown" : "";
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
