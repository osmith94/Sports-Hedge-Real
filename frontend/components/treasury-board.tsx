"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import {
  PaperLedgerReconciliation,
  PaperTreasurySnapshot,
  getPaperLedgerReconciliation,
  resetPaperSession,
} from "../lib/api";
import { money } from "../lib/format";
import { HydratedRelativeTime } from "./hydrated-relative-time";

const VENUE_LABEL: Record<string, string> = {
  matchbook: "Matchbook",
  polymarket: "Polymarket",
  smarkets: "Smarkets",
  kalshi: "Kalshi",
};

function cash(value: string | number | null | undefined, currency: string): string {
  return money(value, currency === "USD" ? "USD" : "GBP");
}

export function TreasuryBoard({
  snapshot,
  available,
}: {
  snapshot: PaperTreasurySnapshot | null;
  available: boolean;
}) {
  const router = useRouter();
  const [saving, setSaving] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reconciliation, setReconciliation] = useState<PaperLedgerReconciliation | null>(null);

  useEffect(() => {
    getPaperLedgerReconciliation()
      .then(setReconciliation)
      .catch(() => setReconciliation(null));
  }, [snapshot?.session?.session_id]);

  async function onReset(event: FormEvent) {
    event.preventDefault();
    if (!confirmReset) {
      setConfirmReset(true);
      setError(null);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await resetPaperSession("explicit operator paper session reset from treasury UI");
      setConfirmReset(false);
      router.refresh();
    } catch (err) {
      const raw = err instanceof Error ? err.message : "Could not reset paper session.";
      setError(`Reset failed: ${raw}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Persistent paper treasury</div>
          <div className="panel-meta">
            Authoritative native venue bankrolls. Same USD on Kalshi and Polymarket is not shared cash.
            GBP carrying values are derived from an explicit FX snapshot.
          </div>
        </div>
        <span className="status-badge">PAPER MODE · HYPOTHETICAL CAPITAL</span>
      </div>
      {!available || !snapshot ? (
        <div className="empty-live-compact">
          Paper treasury API unavailable. No fixture balances are substituted.
        </div>
      ) : (
        <>
          <div className="scan-note">
            {snapshot.note} Session {snapshot.session?.session_id ?? "none"}. Seed £
            {snapshot.session?.seed_gbp ?? "—"} at {snapshot.session?.fx_source ?? "no FX"} (
            {String(snapshot.session?.fx_rate_usd_gbp ?? "—")} GBP per USD).
          </div>
          <div className="pool-grid">
            {snapshot.pools.map((pool) => (
              <div className={`pool-card currency-${pool.native_currency.toLowerCase()}`} key={pool.pool_id}>
                <div className="pool-top">
                  <div>
                    <div className="pool-venue">{VENUE_LABEL[pool.venue] ?? pool.venue}</div>
                    <div className="pool-ccy">{pool.identity} · PAPER CAPITAL</div>
                  </div>
                  <span className="status-badge">{pool.native_currency}</span>
                </div>
                <div className="pool-rows">
                  <div><span>Seed</span><strong>{cash(pool.seed_native, pool.native_currency)}</strong></div>
                  <div><span>Available</span><strong>{cash(pool.available_cash, pool.native_currency)}</strong></div>
                  <div><span>Locked</span><strong>{cash(pool.locked_capital, pool.native_currency)}</strong></div>
                  <div><span>Realised P&L</span><strong>{cash(pool.realised_pnl_native, pool.native_currency)}</strong></div>
                  <div><span>Fees</span><strong>{cash(pool.cumulative_fees_native, pool.native_currency)}</strong></div>
                </div>
                <div className="pool-gbp">
                  {pool.gbp_carrying_status === "fx_unavailable"
                    ? "GBP carrying unavailable"
                    : `${cash(pool.gbp_carrying_value, "GBP")} · ${pool.fx_source ?? "FX"}`}
                </div>
              </div>
            ))}
          </div>
          <div className="pool-actions">
            <button className="pool-reset" type="submit" form="treasury-reset" disabled={saving}>
              {confirmReset
                ? saving
                  ? "Resetting…"
                  : "Confirm reset demo session"
                : "Reset demo session"}
            </button>
          </div>
          <form id="treasury-reset" onSubmit={(event) => void onReset(event)}>
            <div className="scan-note">
              {confirmReset
                ? "Destructive paper-session reset. Remaining demo locks are released at zero betting P&L, active paper trades are abandoned (not a market settlement), identities are archived, and a fresh treasury session opens at configured defaults. Journal/history is retained. PAPER CAPITAL is hypothetical. Confirm above to proceed, or cancel."
                : "Reset opens a new auditable session and seeds £1,000 / USD equivalent. Remaining paper locks are released at zero betting P&L first; this is not a market settlement. Prior journal and treasury events are retained."}
              {confirmReset ? (
                <button
                  className="pool-link"
                  type="button"
                  onClick={() => setConfirmReset(false)}
                  disabled={saving}
                  style={{ marginLeft: 8 }}
                >
                  Cancel
                </button>
              ) : null}
            </div>
          </form>
          {error ? (
            <div className="scan-message scan-message-error" role="alert">
              {error}
            </div>
          ) : null}
          {reconciliation ? (
            <div className="scan-note" style={{ marginBottom: 12 }}>
              Ledger reconstruction {reconciliation.ok ? "OK" : "NOT OK"} · {reconciliation.data_kind} ·
              journals {reconciliation.journal_count} · treasury events {reconciliation.treasury_event_count} ·
              GBP journals {reconciliation.gbp_journals_balanced ? "balanced" : "unbalanced"}
              {reconciliation.deferred.length ? ` · deferred ${reconciliation.deferred.join("; ")}` : ""}
              {reconciliation.mismatches.length ? ` · mismatches ${reconciliation.mismatches.join("; ")}` : ""}.
              Native available/locked reconstruct from the append-only paper journal. PAPER MODE · not a production GL.
            </div>
          ) : null}
          <div className="pool-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Event</th>
                  <th>Venue</th>
                  <th>Native</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.events.length === 0 ? (
                  <tr>
                    <td colSpan={5}>No treasury events yet.</td>
                  </tr>
                ) : (
                  snapshot.events.map((event) => (
                    <tr key={event.event_id}>
                      <td className="muted">
                        <HydratedRelativeTime iso={event.occurred_at} />
                      </td>
                      <td>{event.event_type}</td>
                      <td>{VENUE_LABEL[event.venue] ?? event.venue}</td>
                      <td>{cash(event.native_amount, event.native_currency)}</td>
                      <td className="muted">{event.reason}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
