"use client";

import Link from "next/link";
import { FormEvent, Fragment, useMemo, useState } from "react";

import {
  PaperTrade,
  PaperTradeBookSummary,
  PaperTradeDetail,
  getPaperTrade,
  settlePaperTrade,
} from "../lib/api";
import { money, relativeTime } from "../lib/format";

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

function nativeLocked(trade: PaperTrade): string {
  const parts = Object.entries(trade.capital_locked_native).map(([currency, amount]) =>
    money(amount, currency === "USD" ? "USD" : "GBP"),
  );
  return parts.length ? parts.join(" · ") : "—";
}

function fixture(trade: PaperTrade): string {
  return trade.fixture_label || `${trade.home_team ?? "Unknown"} v ${trade.away_team ?? "Unknown"}`;
}

function stakeLabel(leg: PaperTrade["legs"][number]): string {
  const ccy = leg.currency === "USD" ? "USD" : "GBP";
  if (leg.fill_kind === "UNFILLED") {
    return `requested ${money(leg.requested_stake, ccy)} unfilled`;
  }
  return money(leg.filled_stake, ccy);
}

function legsLine(trade: PaperTrade): string {
  if (!trade.legs.length) return "No legs recorded";
  return trade.legs
    .map((leg) => {
      const odds = leg.filled_odds ?? leg.displayed_odds;
      const mode = leg.execution_mode === "EXTERNAL_OPERATOR" ? " · EXTERNAL_OPERATOR" : "";
      return `${leg.venue} ${leg.outcome} ${odds ?? "—"} × ${stakeLabel(leg)} (${leg.fill_kind}${mode})`;
    })
    .join(" · ");
}

export function PaperTradeBook({ summary, active, closed, apiAvailable, compact = false }: Props) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<PaperTradeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

  async function onSettle(event: FormEvent<HTMLFormElement>, trade: PaperTrade) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const winning = String(form.get("winning_outcome") || "").trim();
    const source = String(form.get("source") || "").trim();
    const sourceId = String(form.get("source_id") || "").trim();
    if (!winning || !source || !sourceId) {
      setError("Settlement requires an explicit outcome, source and source id.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const settled = await settlePaperTrade(trade.trade_id, {
        winning_outcome: winning,
        source,
        source_id: sourceId,
        detail: "Operator-recorded paper settlement; not inferred from kickoff",
        provenance: "fixture_demo",
      });
      setDetail(settled);
      window.location.reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Settlement failed");
    } finally {
      setBusy(false);
    }
  }

  if (!apiAvailable) {
    return (
      <div className="empty-live">
        Paper trade APIs are unavailable. This page does not fall back to mock portfolio figures.
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
        onSettle={onSettle}
        empty="No persisted active paper trades."
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
  onSettle,
  empty,
}: {
  title: string;
  meta: string;
  rows: PaperTrade[];
  openId: string | null;
  detail: PaperTradeDetail | null;
  busy?: boolean;
  onToggle: (id: string) => void;
  onSettle?: (event: FormEvent<HTMLFormElement>, trade: PaperTrade) => void;
  empty: string;
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
        <table>
          <thead>
            <tr>
              <th>Fixture / market</th>
              <th>Opened</th>
              <th>Legs</th>
              <th>Locked capital</th>
              <th>Risk at entry</th>
              <th>Guaranteed at open</th>
              <th>Realised P&L</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={8} className="empty-live">{empty}</td>
              </tr>
            ) : (
              rows.map((trade) => (
                <Fragment key={trade.trade_id}>
                  <tr>
                    <td className="row-title">
                      <button type="button" className="text-link" onClick={() => onToggle(trade.trade_id)}>
                        {fixture(trade)}
                      </button>
                      <div className="panel-meta">
                        {trade.market_label ?? trade.market_family ?? "—"}
                        {trade.solver_model ? ` · ${trade.solver_model}` : ""}
                      </div>
                      <Link href={`/paper/${encodeURIComponent(trade.trade_id)}`} className="panel-meta">
                        Open detail
                      </Link>
                    </td>
                    <td>
                      {relativeTime(trade.opened_at)}
                      {trade.settled_at ? <div className="panel-meta">Settled {relativeTime(trade.settled_at)}</div> : null}
                    </td>
                    <td>{legsLine(trade)}</td>
                    <td>{nativeLocked(trade)}</td>
                    <td title={riskTooltip(trade.entry_risk)}>{riskAtEntry(trade)}</td>
                    <td>{money(trade.guaranteed_profit_gbp_at_open)}</td>
                    <td>{trade.state === "CLOSED" ? money(trade.realised_pnl_gbp) : "—"}</td>
                    <td>
                      <span className="status-badge">{trade.state}</span>
                    </td>
                  </tr>
                  {openId === trade.trade_id && detail?.trade_id === trade.trade_id ? (
                    <tr>
                      <td colSpan={8}>
                        <AuditBlock trade={detail} busy={busy} onSettle={onSettle} />
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
  onSettle,
}: {
  trade: PaperTradeDetail;
  busy?: boolean;
  onSettle?: (event: FormEvent<HTMLFormElement>, trade: PaperTrade) => void;
}) {
  return (
    <div className="paper-audit">
      <div className="panel-meta">
        Provenance {trade.provenance} · paper only · places_orders={String(trade.places_orders ?? false)}
        {trade.settlement_outcome
          ? ` · settled ${trade.settlement_outcome} via ${trade.settlement_source}:${trade.settlement_source_id}`
          : ""}
      </div>
      <ol>
        {trade.audit.map((event) => (
          <li key={event.event_id}>
            <strong>{event.event_type}</strong> · {relativeTime(event.occurred_at)} · {event.detail ?? "—"}
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
      {onSettle && trade.state !== "CLOSED" && trade.state !== "AWAITING_MANUAL_EXTERNAL" ? (
        <form className="scan-form" onSubmit={(event) => onSettle(event, trade)}>
          <div className="panel-meta">Explicit paper settlement. Do not invent a result from elapsed kickoff.</div>
          <div className="scan-control-grid" style={{ marginTop: 8 }}>
            <label>
              Winning outcome
              <select name="winning_outcome" defaultValue={trade.legs[0]?.outcome ?? ""}>
                {trade.legs.map((leg) => (
                  <option key={`${leg.venue}-${leg.outcome}`} value={leg.outcome}>
                    {leg.outcome}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Source
              <input name="source" placeholder="fixture_test" defaultValue="fixture_test" />
            </label>
            <label>
              Source id
              <input name="source_id" placeholder="unique-result-id" required />
            </label>
            <button className="scan-button" type="submit" disabled={busy}>
              {busy ? "Settling…" : "Settle paper trade"}
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}
