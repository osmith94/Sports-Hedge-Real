"use client";

import Link from "next/link";
import { Fragment, useEffect, useMemo, useState } from "react";

import {
  PaperSettlementOptions,
  PaperTrade,
  PaperTradeBookSummary,
  PaperTradeDetail,
  getPaperSettlementOptions,
  getPaperTrade,
  manualSettlePaperTrade,
} from "../lib/api";
import { money } from "../lib/format";
import {
  exitBlockReasonText,
  formatCurrentExit,
  formatEntryArb,
  formatExitDelta,
} from "../lib/active-trade-exit-display";
import { formatPositionManagementCell, managementBadgeClass } from "../lib/paper-position-management-display";
import { compactLegLines, compactMarketHeading, NBA_SETTLEMENT_CAVEAT_TEXT, NFL_SETTLEMENT_CAVEAT_TEXT, tradeShowsNbaSettlementCaveat, tradeShowsNflSettlementCaveat } from "../lib/paper-trade-display";
import { settlementReconciliationLabel } from "../lib/settlement-reconciliation-display";
import { ActiveTradeLog } from "./active-trade-log";
import { HydratedRelativeTime } from "./hydrated-relative-time";

type Props = {
  summary: PaperTradeBookSummary | null;
  active: PaperTrade[];
  closed: PaperTrade[];
  apiAvailable: boolean;
  compact?: boolean;
};

function riskAtEntry(trade: PaperTrade): string {
  const snapshot = trade.entry_risk;
  if (!snapshot || snapshot.score == null) return "—";
  const band = snapshot.band ? ` · ${snapshot.band}` : "";
  return `${snapshot.score}${band}`;
}

function riskTooltip(snapshot: PaperTrade["entry_risk"]): string {
  if (!snapshot) return "No execution-time risk snapshot.";
  const bits = [
    snapshot.reasons?.length ? `reasons ${snapshot.reasons.join(", ")}` : null,
    snapshot.spread_bps != null ? `spread ${snapshot.spread_bps}` : null,
    snapshot.size_to_depth_ratio != null ? `size/depth ${snapshot.size_to_depth_ratio}` : null,
    snapshot.quote_age_ms != null ? `quote age ${snapshot.quote_age_ms}ms` : null,
    snapshot.hedge_liquidity_ratio != null ? `hedge ${snapshot.hedge_liquidity_ratio}` : null,
    snapshot.assumed_latency_ms != null ? `latency ${snapshot.assumed_latency_ms}ms` : null,
    snapshot.maximum_execution_risk != null ? `threshold ${snapshot.maximum_execution_risk}` : null,
  ].filter(Boolean);
  return bits.join(" · ") || "Execution-time risk snapshot";
}

function managementHint(trade: PaperTrade): string {
  const snapshot = trade.position_management;
  if (!snapshot) return "No position-management evaluation yet.";
  const bits = [
    snapshot.capital_pressure ? `capital ${snapshot.capital_pressure}` : null,
    snapshot.opportunity_cost_gbp != null ? `opp-cost ${money(snapshot.opportunity_cost_gbp)}` : null,
    snapshot.quote_age_ms != null ? `age ${snapshot.quote_age_ms}ms` : null,
    snapshot.auto_action === "unwind_pending_confirmation"
      ? "awaiting newer reverse-book confirmation"
      : null,
    snapshot.exit_margin_basis && snapshot.exit_margin_basis !== "unavailable"
      ? `exit-margin basis ${snapshot.exit_margin_basis.replaceAll("_", " ")}`
      : null,
    "exit margin is modelled economic distance to the unwind threshold, not realised P&L",
    snapshot.exit_margin_actionable === false && snapshot.recommendation === "UNWIND_NOT_SAFE"
      ? "positive economic margin is not permission to close"
      : null,
    snapshot.decision_reason?.replaceAll("_", " "),
  ].filter(Boolean);
  return bits.join(" · ");
}

function compactManagement(trade: PaperTrade): { state: string; tone: "ready" | "waiting" | "unsafe" | "neutral"; detail: string } {
  const cell = formatPositionManagementCell(trade.position_management);
  const exitText = formatCurrentExit(trade);
  const block = exitBlockReasonText(trade);
  if (!trade.position_management && exitText === "—") {
    return { state: "—", tone: "neutral", detail: block ?? "no evaluation" };
  }
  if (cell.tone === "unsafe") {
    return { state: "CLOSE BLOCKED", tone: "unsafe", detail: block ?? cell.blocker ?? "unsafe close" };
  }
  if (exitText !== "—") {
    return { state: "CLOSE AVAILABLE", tone: "ready", detail: exitText };
  }
  return { state: "CLOSE BLOCKED", tone: "waiting", detail: block ?? "incomplete close plan" };
}

function ManagementCell({ trade, compact = false }: { trade: PaperTrade; compact?: boolean }) {
  const cell = formatPositionManagementCell(trade.position_management);
  if (compact) {
    const brief = compactManagement(trade);
    return (
      <td className="paper-trade-management" title={managementHint(trade)}>
        <span className={managementBadgeClass(brief.tone)}>{brief.state}</span>
        <div className="panel-meta">
          {brief.detail}
          {cell.checkedIso ? (
            <>
              {" · "}
              <HydratedRelativeTime iso={cell.checkedIso} prefix="checked" />
            </>
          ) : null}
        </div>
      </td>
    );
  }
  return (
    <td className="paper-trade-management" title={managementHint(trade)}>
      <span className={managementBadgeClass(cell.tone)}>{cell.state}</span>
      {cell.checkedIso ? (
        <div className="panel-meta">
          <HydratedRelativeTime iso={cell.checkedIso} prefix="checked" />
        </div>
      ) : null}
      <div className="panel-meta">{cell.economics}</div>
      {cell.threshold ? <div className="panel-meta">{cell.threshold}</div> : null}
      <div className="panel-meta">{cell.margin}</div>
      {cell.blocker ? <div className="panel-meta">{cell.blocker}</div> : null}
      <div className="panel-meta">{cell.release}</div>
    </td>
  );
}

function ExitEvidence({ trade }: { trade: PaperTrade }) {
  const cell = formatPositionManagementCell(trade.position_management);
  return (
    <div className="paper-audit-management">
      <div className="panel-meta">Entry Arb % {formatEntryArb(trade)} · stored opening net edge</div>
      <div className="panel-meta">Current Exit % {formatCurrentExit(trade)}</div>
      <div className="panel-meta">Δ {formatExitDelta(trade)}</div>
      {exitBlockReasonText(trade) ? <div className="panel-meta">{exitBlockReasonText(trade)}</div> : null}
      <div className="panel-meta">{cell.economics}</div>
      {cell.threshold ? <div className="panel-meta">{cell.threshold}</div> : null}
      <div className="panel-meta">{cell.margin}</div>
      {cell.blocker ? <div className="panel-meta">{cell.blocker}</div> : null}
      <div className="panel-meta">{cell.release}</div>
    </div>
  );
}

function nativeLocked(trade: PaperTrade): string {
  const parts = Object.entries(trade.capital_locked_native).map(([currency, amount]) =>
    money(amount, currency === "USD" ? "USD" : "GBP"),
  );
  return parts.length ? parts.join(" · ") : "—";
}

function fixture(trade: PaperTrade): string {
  return trade.fixture_label || `${trade.home_team ?? "Unknown"} v ${trade.away_team ?? "Unknown"}`;
}

function tradeStateBadgeClass(state: PaperTrade["state"]): string {
  if (state === "PENDING" || state === "PARTIAL" || state === "AWAITING_MANUAL_EXTERNAL") {
    return "status-badge status-badge-warn";
  }
  return "status-badge";
}

function LegsCell({ trade }: { trade: PaperTrade }) {
  return (
    <td className="paper-trade-legs">
      {compactLegLines(trade).map((line) => (
        <div key={line} className="paper-trade-leg" title={line}>
          {line}
        </div>
      ))}
    </td>
  );
}

export function PaperTradeBook({ summary, active, closed, apiAvailable, compact = false }: Props) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<PaperTradeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [manualTrade, setManualTrade] = useState<PaperTrade | null>(null);

  const headline = useMemo(() => {
    if (!summary) return null;
    return summary;
  }, [summary]);

  async function toggle(tradeId: string) {
    if (openId === tradeId) {
      setOpenId(null);
      return;
    }
    setError(null);
    setOpenId(tradeId);
    try {
      setDetail(await getPaperTrade(tradeId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load trade detail");
    }
  }

  async function onManualClose(trade: PaperTrade) {
    setError(null);
    setManualTrade(trade);
  }

  async function onConfirmManual(winning: string, note: string) {
    if (!manualTrade) return;
    setBusy(true);
    setError(null);
    try {
      await manualSettlePaperTrade(manualTrade.trade_id, {
        winning_outcome: winning,
        operator_note: note || undefined,
      });
      setManualTrade(null);
      window.location.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Manual settlement failed");
    } finally {
      setBusy(false);
    }
  }

  if (!apiAvailable) {
    return (
      <div className="empty-live">
        Paper trade book could not be loaded. Treasury above is still authoritative.
      </div>
    );
  }

  return (
    <>
      {compact ? null : (
      <section className="metric-grid">
        <div className="metric-card">
          <div className="metric-label">Open paper trades</div>
          <div className="metric-value">{headline?.open_count ?? 0}</div>
          <div className="metric-foot">Persisted paper records only</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Native capital locked</div>
          <div className="metric-value" style={{ fontSize: 18 }}>
            {headline && Object.keys(headline.capital_locked_native).length
              ? Object.entries(headline.capital_locked_native)
                  .map(([ccy, amt]) => money(amt, ccy === "USD" ? "USD" : "GBP"))
                  .join(" · ")
              : "—"}
          </div>
          <div className="metric-foot">GBP and USD kept separate</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Locked GBP reporting</div>
          <div className="metric-value">{money(headline?.capital_locked_gbp)}</div>
          <div className="metric-foot">
            {headline?.gbp_unavailable_reason
              ? `GBP unavailable · ${headline.gbp_unavailable_reason}`
              : "Backend FX carrying value"}
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Realised P&L (GBP)</div>
          <div className={`metric-value ${Number(headline?.realised_pnl_gbp ?? 0) >= 0 ? "metric-positive" : ""}`}>
            {money(headline?.realised_pnl_gbp)}
          </div>
          <div className="metric-foot">{headline?.closed_count ?? 0} closed trades</div>
        </div>
      </section>
      )}

      {error ? <div className="empty-live" style={{ marginBottom: 12 }}>{error}</div> : null}

      <TradeTable
        title="Active trades"
        meta="Open paper trades and native capital locked. PAPER MODE records only."
        rows={active}
        openId={openId}
        detail={detail}
        busy={busy}
        onToggle={toggle}
        onManualClose={onManualClose}
        empty="No persisted active paper trades."
        compact={compact}
      />

      {compact ? null : (
        <>
          <div style={{ height: 14 }} />
          <TradeTable
            title="Closed trades"
            meta="Settled paper history with realised P&L. Settlement is never inferred from kickoff time."
            rows={closed}
            openId={openId}
            detail={detail}
            busy={busy}
            onToggle={toggle}
            empty="No closed paper trades yet."
          />
        </>
      )}
      {manualTrade ? (
        <ManualSettleDialog
          trade={manualTrade}
          busy={busy}
          onClose={() => setManualTrade(null)}
          onConfirm={onConfirmManual}
        />
      ) : null}
    </>
  );
}

function TradeTable({
  title,
  meta,
  rows,
  openId,
  detail,
  busy,
  onToggle,
  onManualClose,
  empty,
  compact = false,
}: {
  title: string;
  meta: string;
  rows: PaperTrade[];
  openId: string | null;
  detail: PaperTradeDetail | null;
  busy?: boolean;
  onToggle: (id: string) => void;
  onManualClose?: (trade: PaperTrade) => void;
  empty: string;
  compact?: boolean;
}) {
  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">{title}</div>
          <div className="panel-meta">{meta}</div>
        </div>
        <span className="demo-chip">PAPER MODE · RECORDED</span>
      </div>
      <div className="table-wrap">
        <table className={compact ? "ops-compact paper-trade-compact" : undefined}>
          <thead>
            <tr>
              {compact ? (
                <>
                  <th>Fixture</th>
                  <th>Market</th>
                  <th>Legs</th>
                  <th>Locked</th>
                  <th>Entry Arb %</th>
                  <th>Guaranteed at Open</th>
                  <th>Current Exit %</th>
                  <th>Δ</th>
                  <th>Management</th>
                  <th>Status</th>
                </>
              ) : (
                <>
                  <th>Fixture / market</th>
                  <th>Opened</th>
                  <th>Legs</th>
                  <th>Locked capital</th>
                  <th>Entry Arb %</th>
                  <th>Risk at entry</th>
                  <th>Guaranteed at open</th>
                  <th>Current Exit %</th>
                  <th>Δ</th>
                  <th>Realised P&L</th>
                  <th>Management</th>
                  <th>Status</th>
                </>
              )}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={compact ? 10 : 12} className="empty-live">{empty}</td>
              </tr>
            ) : (
              rows.map((trade) => (
                <Fragment key={trade.trade_id}>
                  <tr>
                    <td className="row-title paper-trade-fixture" title={fixture(trade)}>
                      <button type="button" className="text-link paper-trade-fixture-name" onClick={() => onToggle(trade.trade_id)}>
                        {fixture(trade)}
                      </button>
                      {compact ? null : (
                        <div className="paper-trade-market" title={trade.solver_model ? `${compactMarketHeading(trade)} · ${trade.solver_model}` : compactMarketHeading(trade)}>
                          {compactMarketHeading(trade)}
                        </div>
                      )}
                      {tradeShowsNflSettlementCaveat(trade) ? (
                        <div className="panel-meta paper-trade-nfl-caveat">{NFL_SETTLEMENT_CAVEAT_TEXT}</div>
                      ) : tradeShowsNbaSettlementCaveat(trade) ? (
                        <div className="panel-meta paper-trade-nfl-caveat">{NBA_SETTLEMENT_CAVEAT_TEXT}</div>
                      ) : null}
                      {compact ? null : (
                        <div className="paper-trade-actions">
                          <Link href={`/paper/${encodeURIComponent(trade.trade_id)}`}>
                            Open detail
                          </Link>
                          {" · "}
                          <Link href={`/paper/${encodeURIComponent(trade.trade_id)}#trade-log`}>
                            Trade log
                          </Link>
                          {onManualClose && trade.state !== "CLOSED" && trade.state !== "AWAITING_MANUAL_EXTERNAL" ? (
                            <>
                              {" · "}
                              <button
                                type="button"
                                className="text-link"
                                onClick={() => onManualClose(trade)}
                              >
                                Manual close / settle result
                              </button>
                            </>
                          ) : null}
                        </div>
                      )}
                    </td>
                    {compact ? (
                      <td className="paper-trade-market">{compactMarketHeading(trade)}</td>
                    ) : (
                      <td>
                        <HydratedRelativeTime iso={trade.opened_at} />
                        {trade.settled_at ? (
                          <div className="panel-meta">
                            Settled <HydratedRelativeTime iso={trade.settled_at} />
                          </div>
                        ) : null}
                      </td>
                    )}
                    <LegsCell trade={trade} />
                    <td>{nativeLocked(trade)}</td>
                    <td>{formatEntryArb(trade)}</td>
                    {compact ? null : <td title={riskTooltip(trade.entry_risk)}>{riskAtEntry(trade)}</td>}
                    <td>{money(trade.guaranteed_profit_gbp_at_open)}</td>
                    <td title={exitBlockReasonText(trade) ?? undefined}>{formatCurrentExit(trade)}</td>
                    <td>{formatExitDelta(trade)}</td>
                    {compact ? null : <td>{trade.state === "CLOSED" ? money(trade.realised_pnl_gbp) : "—"}</td>}
                    <ManagementCell trade={trade} compact={compact} />
                    <td>
                      <span className={tradeStateBadgeClass(trade.state)}>{trade.state}</span>
                      {settlementReconciliationLabel(trade) ? (
                        <div className="panel-meta" title={trade.settlement_blocker_detail ?? undefined}>
                          {settlementReconciliationLabel(trade)}
                        </div>
                      ) : null}
                      {compact || !trade.last_settlement_check_at ? null : (
                        <div className="panel-meta">
                          Settlement check <HydratedRelativeTime iso={trade.last_settlement_check_at} />
                        </div>
                      )}
                    </td>
                  </tr>
                  {openId === trade.trade_id && detail?.trade_id === trade.trade_id ? (
                    <tr>
                      <td colSpan={compact ? 10 : 12}>
                        <AuditBlock trade={detail} busy={busy} onManualClose={onManualClose} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function AuditBlock({
  trade,
  busy,
  onManualClose,
}: {
  trade: PaperTradeDetail;
  busy?: boolean;
  onManualClose?: (trade: PaperTrade) => void;
}) {
  const recon = settlementReconciliationLabel(trade);
  return (
    <div className="paper-audit">
      <ExitEvidence trade={trade} />
      <div className="paper-trade-actions">
        <Link href={`/paper/${encodeURIComponent(trade.trade_id)}`}>
          Open detail
        </Link>
        {" · "}
        <Link href={`/paper/${encodeURIComponent(trade.trade_id)}#trade-log`}>
          Trade log
        </Link>
      </div>
      <div className="panel-meta">
        Provenance {trade.provenance} · paper only · no venue orders
        {trade.settlement_outcome
          ? ` · settled ${trade.settlement_outcome} via ${trade.settlement_source}:${trade.settlement_source_id}`
          : ""}
      </div>
      {recon ? <div className="panel-meta">{recon}</div> : null}
      {trade.last_settlement_check_at ? (
        <div className="panel-meta">
          Last auto-settlement check <HydratedRelativeTime iso={trade.last_settlement_check_at} />
        </div>
      ) : null}
      <ul>
        {trade.legs.map((leg) => (
          <li key={`${leg.venue}-${leg.outcome}-${leg.source_market_id}`}>
            {leg.venue} · {leg.outcome} · {leg.fill_kind} · odds {leg.filled_odds ?? leg.displayed_odds ?? "—"}
            {leg.filled_stake != null ? ` · stake ${leg.filled_stake}` : ""}
          </li>
        ))}
      </ul>
      <ActiveTradeLog presetTradeId={trade.trade_id} compact />
      <ol>
        {trade.audit.map((event) => (
          <li key={event.event_id}>
            <strong>{event.event_type}</strong> · <HydratedRelativeTime iso={event.occurred_at} /> · {event.detail ?? "—"}
          </li>
        ))}
      </ol>
      {trade.journals?.length ? (
        <div>
          <div className="panel-title">Accounting entries</div>
          <ul>
            {trade.journals.map((entry) => (
              <li key={entry.journal_id}>
                {entry.source}/{entry.source_id}: {entry.description}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {onManualClose && trade.state !== "CLOSED" && trade.state !== "AWAITING_MANUAL_EXTERNAL" ? (
        <div className="scan-control-grid" style={{ marginTop: 8 }}>
          <button className="scan-button" type="button" disabled={busy} onClick={() => onManualClose(trade)}>
            Manual close / settle result
          </button>
        </div>
      ) : null}
    </div>
  );
}

function ManualSettleDialog({
  trade,
  busy,
  onClose,
  onConfirm,
}: {
  trade: PaperTrade;
  busy: boolean;
  onClose: () => void;
  onConfirm: (winning: string, note: string) => Promise<void>;
}) {
  const [options, setOptions] = useState<PaperSettlementOptions | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [winning, setWinning] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    setOptions(null);
    getPaperSettlementOptions(trade.trade_id)
      .then((payload) => {
        if (cancelled) return;
        setOptions(payload);
        setWinning(payload.choices[0]?.value ?? "");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "Unable to load settlement choices");
      });
    return () => {
      cancelled = true;
    };
  }, [trade.trade_id]);

  const selected = options?.choices.find((choice) => choice.value === winning) ?? null;

  return (
    <div className="competition-modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="competition-modal settlement-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="manual-settle-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="competition-modal-header">
          <div>
            <div className="competition-modal-kicker">PAPER MODE · FAILSAFE</div>
            <h2 id="manual-settle-title">Manual close / settle result</h2>
            <p>
              Record the actual canonical market result. Sports Hedge will close this PAPER
              trade using stored filled odds, stakes and fees — never the current quote.
              Settlement is not inferred from elapsed kickoff.
            </p>
          </div>
          <button type="button" className="scan-button" onClick={onClose}>
            Cancel
          </button>
        </div>
        <div className="panel-meta" style={{ marginTop: 10 }}>
          {options?.fixture_label || fixture(trade)} · {compactMarketHeading(trade)}
        </div>
        <ul>
          {(options?.legs ?? trade.legs).map((leg) => (
            <li key={`${leg.venue}-${leg.outcome}`}>
              {leg.venue} · {leg.outcome} · stake {leg.filled_stake} {leg.currency} · odds{" "}
              {"filled_odds" in leg ? (leg.filled_odds ?? leg.displayed_odds ?? "—") : "—"}
            </li>
          ))}
        </ul>
        {loadError ? <div className="empty-live">{loadError}</div> : null}
        {options?.unsupported_reason ? (
          <div className="empty-live">
            This market family cannot be manually settled ({options.unsupported_reason}).
          </div>
        ) : null}
        {options && !options.unsupported_reason ? (
          <>
            <label className="scan-field">
              Actual canonical market result
              <select value={winning} onChange={(event) => setWinning(event.target.value)}>
                {options.choices.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.label}
                  </option>
                ))}
              </select>
            </label>
            {selected?.realised_pnl_gbp != null ? (
              <div className="panel-meta" style={{ marginTop: 8 }}>
                Preview realised P&amp;L from stored fills: {money(selected.realised_pnl_gbp)}
              </div>
            ) : null}
            <label className="scan-field" style={{ marginTop: 10 }}>
              Operator note (optional)
              <input value={note} onChange={(event) => setNote(event.target.value)} />
            </label>
            <div className="competition-modal-actions">
              <button type="button" className="scan-button" onClick={onClose} disabled={busy}>
                Cancel
              </button>
              <button
                type="button"
                className="scan-button"
                disabled={busy || !winning}
                onClick={() => onConfirm(winning, note)}
              >
                {busy ? "Closing…" : "Confirm result & close trade"}
              </button>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
