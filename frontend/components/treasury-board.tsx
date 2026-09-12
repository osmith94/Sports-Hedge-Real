"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

import {
  PaperTreasurySnapshot,
  resetPaperTreasury,
} from "../lib/api";
import { money, relativeTime } from "../lib/format";

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
  const [error, setError] = useState<string | null>(null);

  async function onReset(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await resetPaperTreasury("operator demo reset from treasury UI");
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reset paper treasury.");
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
              {saving ? "Resetting…" : "Reset demo session"}
            </button>
          </div>
          <form id="treasury-reset" onSubmit={(event) => void onReset(event)}>
            <div className="scan-note">
              Reset opens a new auditable session and seeds £1,000 / USD equivalent. Prior journal
              and treasury events are retained.
            </div>
          </form>
          {error ? (
            <div className="scan-message scan-message-error" role="alert">
              {error}
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
                      <td className="muted">{relativeTime(event.occurred_at)}</td>
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
