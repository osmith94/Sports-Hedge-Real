"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

import {
  PaperLiquidityPool,
  PaperLiquiditySnapshot,
  PaperTreasuryPool,
  PaperTreasurySnapshot,
  resetPaperLiquidityPools,
  savePaperTreasuryPools,
} from "../lib/api";
import { money } from "../lib/format";

const VENUE_LABEL: Record<string, string> = {
  matchbook: "Matchbook",
  polymarket: "Polymarket",
  smarkets: "Smarkets",
  kalshi: "Kalshi",
};

const FIRST_CLASS = ["matchbook", "polymarket", "kalshi"] as const;

function statusLabel(pool: PaperLiquidityPool): string {
  if (pool.venue === "smarkets" || !pool.included_in_solver) {
    return "not connected · excluded from solver";
  }
  return "connected · paper solver";
}

function carryingFromLiquidity(pool: PaperLiquidityPool): string {
  if (pool.gbp_carrying_status === "fx_unavailable") {
    return "GBP carrying unavailable · no backend FX";
  }
  const source = pool.gbp_fx_source === "functional_currency" ? "GBP identity" : pool.gbp_fx_source ?? "FX";
  return `${money(pool.gbp_carrying_value)} · ${source}`;
}

function carryingFromTreasury(pool: PaperTreasuryPool): string {
  if (pool.gbp_carrying_status === "fx_unavailable") {
    return "GBP carrying unavailable · no backend FX";
  }
  const rate = pool.fx_rate_gbp_per_unit != null ? ` @ ${pool.fx_rate_gbp_per_unit}` : "";
  const asOf = pool.fx_as_of ? ` · ${pool.fx_as_of}` : "";
  return `${money(pool.gbp_carrying_value)} · ${pool.fx_source ?? "FX"}${rate}${asOf}`;
}

export function LiquidityPools({
  snapshot,
  available,
  treasury = null,
  treasuryAvailable = false,
  compact = false,
}: {
  snapshot: PaperLiquiditySnapshot | null;
  available: boolean;
  treasury?: PaperTreasurySnapshot | null;
  treasuryAvailable?: boolean;
  compact?: boolean;
}) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});

  const useTreasury = Boolean(treasuryAvailable && treasury);
  const firstClassLiquidity = (snapshot?.pools ?? []).filter((pool) =>
    FIRST_CLASS.includes(pool.venue as (typeof FIRST_CLASS)[number]),
  );
  const deferred = (snapshot?.pools ?? []).filter((pool) => pool.venue === "smarkets");
  const treasuryRows = (treasury?.pools ?? []).filter((pool) =>
    FIRST_CLASS.includes(pool.venue as (typeof FIRST_CLASS)[number]),
  );

  function startEdit() {
    if (useTreasury && treasury) {
      setDraft(
        Object.fromEntries(treasuryRows.map((pool) => [pool.venue, String(pool.available_cash)])),
      );
    } else if (snapshot) {
      setDraft(
        Object.fromEntries(firstClassLiquidity.map((pool) => [pool.venue, String(pool.available)])),
      );
    } else {
      return;
    }
    setError(null);
    setEditing(true);
  }

  async function onSave(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const venues = useTreasury
        ? treasuryRows.map((pool) => pool.venue)
        : firstClassLiquidity.map((pool) => pool.venue);
      await savePaperTreasuryPools(
        venues.map((venue) => ({
          venue,
          available: draft[venue] ?? "0",
        })),
      );
      setEditing(false);
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save paper treasury.");
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

  const fxNote = treasury?.session
    ? `FX ${treasury.session.fx_source} ${treasury.session.fx_rate_usd_gbp} GBP/USD as of ${treasury.session.fx_as_of}`
    : snapshot?.pools.find((pool) => pool.gbp_fx_source)?.gbp_fx_source
      ? `FX ${snapshot.pools.find((pool) => pool.gbp_fx_source)?.gbp_fx_source}`
      : "FX provenance from paper seed or live snapshot";

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Paper Treasury</div>
          <div className="panel-meta">
            Hypothetical native standing capital. Matchbook GBP, Polymarket USD and Kalshi USD stay separate.
            {` ${fxNote}.`}
          </div>
        </div>
        <span className="status-badge">PAPER CAPITAL · HYPOTHETICAL</span>
      </div>
      {(!available || !snapshot) && !useTreasury ? (
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
                    <th>GBP carrying</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {useTreasury
                    ? treasuryRows.map((pool) => (
                        <tr key={pool.venue}>
                          <td className="row-title">
                            {VENUE_LABEL[pool.venue] ?? pool.venue}
                            <div className="muted">{pool.native_currency} native</div>
                          </td>
                          <td>{money(pool.available_cash, pool.native_currency === "USD" ? "USD" : "GBP")}</td>
                          <td>{money(pool.locked_capital, pool.native_currency === "USD" ? "USD" : "GBP")}</td>
                          <td className="muted wrap">{carryingFromTreasury(pool)}</td>
                          <td>
                            <span className="status-badge">connected · paper solver</span>
                          </td>
                        </tr>
                      ))
                    : firstClassLiquidity.map((pool) => (
                        <tr key={pool.venue}>
                          <td className="row-title">
                            {VENUE_LABEL[pool.venue] ?? pool.venue}
                            <div className="muted">{pool.native_currency} native</div>
                          </td>
                          <td>{money(pool.available, pool.native_currency)}</td>
                          <td>{money(pool.locked, pool.native_currency)}</td>
                          <td className="muted wrap">{carryingFromLiquidity(pool)}</td>
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
              (useTreasury ? treasuryRows : firstClassLiquidity).map((pool) => {
                if ("available_cash" in pool) {
                  const row = pool as PaperTreasuryPool;
                  return (
                    <div className={`pool-card currency-${row.native_currency.toLowerCase()}`} key={row.venue}>
                      <div className="pool-top">
                        <div>
                          <div className="pool-venue">{VENUE_LABEL[row.venue] ?? row.venue}</div>
                          <div className="pool-ccy">{row.native_currency} native · PAPER CAPITAL</div>
                        </div>
                        <span className="status-badge">connected · paper solver</span>
                      </div>
                      <div className="pool-rows">
                        <div><span>Available</span><strong>{money(row.available_cash, row.native_currency === "USD" ? "USD" : "GBP")}</strong></div>
                        <div><span>Locked</span><strong>{money(row.locked_capital, row.native_currency === "USD" ? "USD" : "GBP")}</strong></div>
                      </div>
                      <div className="pool-gbp">{carryingFromTreasury(row)}</div>
                    </div>
                  );
                }
                const row = pool as PaperLiquidityPool;
                return (
                  <div className={`pool-card currency-${row.native_currency.toLowerCase()}`} key={row.venue}>
                    <div className="pool-top">
                      <div>
                        <div className="pool-venue">{VENUE_LABEL[row.venue] ?? row.venue}</div>
                        <div className="pool-ccy">{row.native_currency} native · PAPER CAPITAL</div>
                      </div>
                      <span className={row.included_in_solver ? "status-badge" : "ops-status is-reject"}>
                        {statusLabel(row)}
                      </span>
                    </div>
                    <div className="pool-rows">
                      <div><span>Available</span><strong>{money(row.available, row.native_currency)}</strong></div>
                      <div><span>Locked</span><strong>{money(row.locked, row.native_currency)}</strong></div>
                      <div><span>Transit</span><strong>{money(row.transit, row.native_currency)}</strong></div>
                    </div>
                    <div className="pool-gbp">{carryingFromLiquidity(row)}</div>
                  </div>
                );
              })
            )}
          </div>
          <div className="pool-actions">
            <button className="scan-button" type="button" onClick={startEdit} disabled={saving}>
              Edit paper amounts
            </button>
            <Link className="pool-link" href="/treasury">Treasury detail</Link>
            <button className="pool-reset" type="button" onClick={() => void onReset()} disabled={saving}>
              Reset defaults
            </button>
          </div>
          {editing ? (
            <form className="pool-editor" onSubmit={(event) => void onSave(event)}>
              <div className="scan-note">PAPER CAPITAL / HYPOTHETICAL · does not represent live venue funds. Edits fail closed while locks or open trades exist.</div>
              <div className="scan-control-grid scan-control-grid-ops">
                {(useTreasury ? treasuryRows : firstClassLiquidity).map((pool) => {
                  const venue = pool.venue;
                  const currency = "native_currency" in pool ? pool.native_currency : "GBP";
                  return (
                    <label className="scan-field" key={venue}>
                      <span>{VENUE_LABEL[venue]} available {currency}</span>
                      <input
                        inputMode="decimal"
                        value={draft[venue] ?? ""}
                        onChange={(event) =>
                          setDraft((current) => ({ ...current, [venue]: event.target.value }))
                        }
                        aria-label={`${venue} paper available ${currency}`}
                      />
                    </label>
                  );
                })}
                <div className="scan-action">
                  <button className="scan-button" type="submit" disabled={saving}>
                    {saving ? "Saving…" : "Save paper treasury"}
                  </button>
                </div>
              </div>
            </form>
          ) : null}
          {deferred.length ? (
            <details className="scan-advanced">
              <summary>Advanced · deferred venues</summary>
              <p className="scan-advanced-copy">
                Smarkets is deferred and excluded from the solver. It is not a first-class paper venue.
              </p>
              {deferred.map((pool) => (
                <div className="muted" key={pool.venue}>
                  {VENUE_LABEL[pool.venue]} {money(pool.available, pool.native_currency)} · {statusLabel(pool)}
                </div>
              ))}
            </details>
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
