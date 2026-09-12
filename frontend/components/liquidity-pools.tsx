"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

import {
  PaperLiquidityPool,
  PaperLiquiditySnapshot,
  resetPaperLiquidityPools,
  savePaperLiquidityPools,
} from "../lib/api";
import { money } from "../lib/format";

const VENUE_LABEL: Record<string, string> = {
  matchbook: "Matchbook",
  polymarket: "Polymarket",
  smarkets: "Smarkets",
  kalshi: "Kalshi",
};

function statusLabel(pool: PaperLiquidityPool): string {
  if (pool.venue === "smarkets" || !pool.included_in_solver) {
    return "not connected · excluded from solver";
  }
  return "connected · paper solver";
}

function carryingText(pool: PaperLiquidityPool): string {
  if (pool.gbp_carrying_status === "fx_unavailable") {
    return "GBP carrying unavailable · no backend FX";
  }
  const source = pool.gbp_fx_source === "functional_currency" ? "GBP identity" : pool.gbp_fx_source ?? "FX";
  return `${money(pool.gbp_carrying_value)} · ${source}`;
}

export function LiquidityPools({
  snapshot,
  available,
  compact = false,
}: {
  snapshot: PaperLiquiditySnapshot | null;
  available: boolean;
  compact?: boolean;
}) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});

  function startEdit() {
    if (!snapshot) return;
    setDraft(
      Object.fromEntries(snapshot.pools.map((pool) => [pool.venue, String(pool.available)])),
    );
    setError(null);
    setEditing(true);
  }

  async function onSave(event: FormEvent) {
    event.preventDefault();
    if (!snapshot) return;
    setSaving(true);
    setError(null);
    try {
      await savePaperLiquidityPools(
        snapshot.pools.map((pool) => ({
          venue: pool.venue,
          available: draft[pool.venue] ?? String(pool.available),
        })),
      );
      setEditing(false);
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save paper pools.");
    } finally {
      setSaving(false);
    }
  }

  async function onReset() {
    setSaving(true);
    setError(null);
    try {
      await resetPaperLiquidityPools();
      setEditing(false);
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reset paper pools.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Paper liquidity pools</div>
          <div className="panel-meta">
            Hypothetical native standing capital. USD and GBP are never summed as one cash figure.
          </div>
        </div>
        <span className="status-badge">PAPER CAPITAL · HYPOTHETICAL</span>
      </div>
      {!available || !snapshot ? (
        <div className="empty-live-compact">Paper liquidity API unavailable. No fixture balances are substituted.</div>
      ) : (
        <>
          <div className={compact ? "pool-table-wrap" : "pool-grid"}>
            {compact ? (
              <table>
                <thead>
                  <tr>
                    <th>Venue</th>
                    <th>Native available</th>
                    <th>Locked</th>
                    <th>Transit</th>
                    <th>GBP carrying</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.pools.map((pool) => (
                    <tr key={pool.venue}>
                      <td className="row-title">
                        {VENUE_LABEL[pool.venue] ?? pool.venue}
                        <div className="muted">{pool.native_currency} native</div>
                      </td>
                      <td>{money(pool.available, pool.native_currency)}</td>
                      <td>{money(pool.locked, pool.native_currency)}</td>
                      <td>{money(pool.transit, pool.native_currency)}</td>
                      <td className="muted">{carryingText(pool)}</td>
                      <td>
                        <span className={pool.included_in_solver ? "status-badge" : "ops-status is-reject"}>
                          {statusLabel(pool)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              snapshot.pools.map((pool) => (
                <div className={`pool-card currency-${pool.native_currency.toLowerCase()}`} key={pool.venue}>
                  <div className="pool-top">
                    <div>
                      <div className="pool-venue">{VENUE_LABEL[pool.venue] ?? pool.venue}</div>
                      <div className="pool-ccy">{pool.native_currency} native · PAPER CAPITAL</div>
                    </div>
                    <span className={pool.included_in_solver ? "status-badge" : "ops-status is-reject"}>
                      {statusLabel(pool)}
                    </span>
                  </div>
                  <div className="pool-rows">
                    <div><span>Available</span><strong>{money(pool.available, pool.native_currency)}</strong></div>
                    <div><span>Locked</span><strong>{money(pool.locked, pool.native_currency)}</strong></div>
                    <div><span>Transit</span><strong>{money(pool.transit, pool.native_currency)}</strong></div>
                  </div>
                  <div className="pool-gbp">{carryingText(pool)}</div>
                </div>
              ))
            )}
          </div>
          <div className="pool-actions">
            <button className="scan-button" type="button" onClick={startEdit} disabled={saving}>
              Edit paper pools
            </button>
            <Link className="pool-link" href="/treasury">Treasury detail</Link>
            <button className="pool-reset" type="button" onClick={() => void onReset()} disabled={saving}>
              Reset defaults
            </button>
          </div>
          {editing ? (
            <form className="pool-editor" onSubmit={(event) => void onSave(event)}>
              <div className="scan-note">PAPER CAPITAL / HYPOTHETICAL · does not represent live venue funds.</div>
              <div className="scan-control-grid scan-control-grid-ops">
                {snapshot.pools.map((pool) => (
                  <label className="scan-field" key={pool.venue}>
                    <span>{VENUE_LABEL[pool.venue]} available {pool.native_currency}</span>
                    <input
                      inputMode="decimal"
                      value={draft[pool.venue] ?? ""}
                      onChange={(event) =>
                        setDraft((current) => ({ ...current, [pool.venue]: event.target.value }))
                      }
                      aria-label={`${pool.venue} paper available ${pool.native_currency}`}
                    />
                  </label>
                ))}
                <div className="scan-action">
                  <button className="scan-button" type="submit" disabled={saving}>
                    {saving ? "Saving…" : "Save paper pools"}
                  </button>
                </div>
              </div>
            </form>
          ) : null}
          {error ? (
            <div className="scan-message scan-message-error" role="alert">
              {error}
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}
