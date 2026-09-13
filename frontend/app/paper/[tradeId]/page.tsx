import Link from "next/link";
import { notFound } from "next/navigation";

import { getPaperTrade } from "../../../lib/api";
import { money, relativeTime } from "../../../lib/format";

export const dynamic = "force-dynamic";

export default async function PaperTradeDetailPage({
  params,
}: {
  params: Promise<{ tradeId: string }>;
}) {
  const { tradeId: rawId } = await params;
  const tradeId = decodeURIComponent(rawId);
  let trade;
  try {
    trade = await getPaperTrade(tradeId);
  } catch {
    notFound();
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Paper trade</div>
          <h1>{trade.fixture_label ?? trade.trade_id}</h1>
          <p className="page-subtitle">
            PAPER MODE persisted record. Solver {trade.solver_model ?? "n/a"}. Provenance {trade.provenance}. Places orders: {String(trade.places_orders)}. Fill kinds stay distinct: INTERNAL_SIMULATED, PAPER_SIMULATED_EXTERNAL, and MANUAL_EXTERNAL are not interchangeable.
          </p>
        </div>
        <Link href="/paper" className="demo-label">Back to trade book</Link>
      </div>
      <section className="metric-grid">
        <div className="metric-card">
          <div className="metric-label">Status</div>
          <div className="metric-value" style={{ fontSize: 18 }}>{trade.state}</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Opened</div>
          <div className="metric-value" style={{ fontSize: 18 }}>{relativeTime(trade.opened_at)}</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Guaranteed at open</div>
          <div className="metric-value">{money(trade.guaranteed_profit_gbp_at_open)}</div>
          <div className="metric-foot">
            {trade.state === "OPEN"
              ? "Recorded only after every required opening leg validated"
              : "Unset until the complete opening hedge is validated"}
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Realised P&L</div>
          <div className="metric-value">{money(trade.realised_pnl_gbp)}</div>
          <div className="metric-foot">
            {trade.settlement_outcome
              ? `${trade.settlement_outcome} · ${trade.settlement_source}`
              : "Unset until explicit settlement"}
          </div>
        </div>
      </section>
      <section className="panel">
        <div className="panel-header">
          <div className="panel-title">Legs</div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Venue</th>
                <th>Outcome</th>
                <th>Fill kind</th>
                <th>Mode</th>
                <th>Odds</th>
                <th>Requested</th>
                <th>Filled</th>
                <th>Capital source</th>
              </tr>
            </thead>
            <tbody>
              {trade.legs.map((leg) => (
                <tr key={`${leg.venue}-${leg.outcome}`}>
                  <td>{leg.venue}</td>
                  <td>{leg.outcome}</td>
                  <td>{leg.fill_kind}</td>
                  <td>{leg.execution_mode}</td>
                  <td>{leg.filled_odds ?? leg.displayed_odds ?? "—"}</td>
                  <td>{money(leg.requested_stake, leg.currency === "USD" ? "USD" : "GBP")}</td>
                  <td>
                    {leg.fill_kind === "UNFILLED"
                      ? "Unfilled"
                      : money(leg.filled_stake, leg.currency === "USD" ? "USD" : "GBP")}
                  </td>
                  <td>{leg.capital_source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <div style={{ height: 14 }} />
      <section className="panel">
        <div className="panel-header">
          <div className="panel-title">Hold vs unwind</div>
          <span className="status-badge">ANALYTICAL · NOT A RELEASE</span>
        </div>
        <div className="panel-body">
          <div className="empty-live">
            Reverse-side close quotes are evaluated on demand. Modelled remaining lock, when shown, is advisory and never spendable. Capital releases only after a validated unwind or explicit settlement posts through the paper treasury.
          </div>
        </div>
      </section>
      <div style={{ height: 14 }} />
      <section className="panel">
        <div className="panel-header">
          <div className="panel-title">Audit trail</div>
        </div>
        <div className="panel-body">
          <ol>
            {trade.audit.map((event) => (
              <li key={event.event_id}>
                {event.event_type} · {relativeTime(event.occurred_at)} · {event.detail ?? "—"}
              </li>
            ))}
          </ol>
        </div>
      </section>
      <div style={{ height: 14 }} />
      <section className="panel">
        <div className="panel-header">
          <div className="panel-title">Paper subledger</div>
        </div>
        <div className="panel-body">
          {trade.journals.length === 0 ? (
            <div className="empty-live">No journal entries for this trade.</div>
          ) : (
            <ul>
              {trade.journals.map((entry) => (
                <li key={entry.journal_id}>
                  {entry.source} / {entry.source_id}: {entry.description} ({entry.postings.length} postings)
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>
    </>
  );
}
