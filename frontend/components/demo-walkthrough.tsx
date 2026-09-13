"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { HoldVsUnwindCard } from "./hold-vs-unwind";
import {
  DemoWalkthroughSnapshot,
  FixtureReplayResult,
  PaperTrade,
  closeDemoTrade,
  getDemoWalkthrough,
  resetDemoWalkthrough,
  runFixtureReplay,
} from "../lib/api";
import { money } from "../lib/format";

const PAIRS: Array<FixtureReplayResult["venue_pair"]> = [
  "matchbook_polymarket",
  "matchbook_kalshi",
  "polymarket_kalshi",
];

function native(amount: string | number | null | undefined, currency: string): string {
  return money(amount, currency === "USD" ? "USD" : "GBP");
}

function fillLine(trade: PaperTrade): string {
  return trade.legs
    .map((leg) => `${leg.venue} ${leg.outcome} ${leg.fill_kind} ${native(leg.filled_stake, leg.currency)}`)
    .join(" · ");
}

export function DemoWalkthroughBoard() {
  const [snapshot, setSnapshot] = useState<DemoWalkthroughSnapshot | null>(null);
  const [replay, setReplay] = useState<FixtureReplayResult | null>(null);
  const [pair, setPair] = useState<FixtureReplayResult["venue_pair"]>("matchbook_polymarket");
  const [solver, setSolver] = useState<FixtureReplayResult["solver"]>("simple");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [available, setAvailable] = useState(true);

  const refresh = useCallback(async () => {
    const next = await getDemoWalkthrough();
    setSnapshot(next);
    setReplay(next.replay ?? null);
    setAvailable(true);
  }, []);

  useEffect(() => {
    refresh().catch((err: unknown) => {
      setAvailable(false);
      setError(err instanceof Error ? err.message : "Demo walkthrough API unavailable");
    });
  }, [refresh]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Demo action failed");
    } finally {
      setBusy(false);
    }
  }

  const liveTriggered = snapshot?.live_triggered.length ?? 0;
  const liveNear = snapshot?.live_near.length ?? 0;
  const discoveryError = snapshot?.discovery?.last_error;
  const active =
    snapshot?.active_trades[0] ??
    (replay?.trade?.state === "OPEN" ? replay.trade : null);
  const closed = replay?.trade?.state === "CLOSED" ? replay.trade : snapshot?.closed_trades[0] ?? null;
  const shownTrade = replay?.trade ?? active ?? closed ?? null;
  const unwind = snapshot?.hold_vs_unwind ?? replay?.unwind;

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Step 9 operator demo</div>
          <h1>Integrated paper-mode walkthrough</h1>
          <p className="page-subtitle">
            Reset three native paper pools, inspect live/read-only discovery without substituting
            fixture rows, then run the same 8F allocator → autofill → 8E treasury → 8D hold/unwind
            lifecycle. Phase 1 remains PAPER MODE. execution_enabled=false.
          </p>
        </div>
        <div className="heading-actions">
          <div className="demo-label">PAPER MODE · NO EXECUTION</div>
          <Link className="pool-link" href="/">Operations console</Link>
        </div>
      </div>

      {!available ? (
        <div className="empty-live">Paper demo API unavailable. No fixture balances are substituted.</div>
      ) : null}
      {error ? <div className="empty-live" style={{ marginBottom: 12 }}>{error}</div> : null}

      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">1. Reset / start demo</div>
            <div className="panel-meta">
              Matchbook GBP £1,000. Polymarket USD and Kalshi USD each represent £1,000 carrying value
              at the demo FX snapshot. Same USD is never commingled.
            </div>
          </div>
        </div>
        <div className="panel-body">
          <div className="demo-actions">
            <button
              className="demo-primary"
              type="button"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  setSnapshot(await resetDemoWalkthrough({ reinitialize_store: false }));
                  setReplay(null);
                })
              }
            >
              Start / seed pools
            </button>
            <button
              className="pool-reset"
              type="button"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  setSnapshot(
                    await resetDemoWalkthrough({
                      reinitialize_store: true,
                      reason: "explicit operator demo store reinitialize",
                    }),
                  );
                  setReplay(null);
                })
              }
            >
              Reinitialize store
            </button>
          </div>
          <p className="section-copy">
            Ordinary reset fails closed while locks or open trades exist. Reinitialize returns remaining
            locks at zero betting P&amp;L and archives identities; it is not a market settlement.
          </p>
          <div className="pool-grid">
            {(snapshot?.pools ?? []).map((pool) => (
              <div className={`pool-card currency-${pool.native_currency.toLowerCase()}`} key={`${pool.venue}-${pool.native_currency}`}>
                <div className="pool-top">
                  <div>
                    <div className="pool-venue">{pool.venue}</div>
                    <div className="pool-ccy">{pool.native_currency} native · GBP carrying is not cash</div>
                  </div>
                  <span className="status-badge">{pool.native_currency}</span>
                </div>
                <div className="pool-figures">
                  <div>Available {native(pool.available_cash, pool.native_currency)}</div>
                  <div>Locked {native(pool.locked_capital, pool.native_currency)}</div>
                  <div>GBP carrying {money(pool.gbp_carrying_value)} · {pool.gbp_carrying_status}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <div style={{ height: 14 }} />
      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">2. Discovery / tracking</div>
            <div className="panel-meta">Read-only live/near-future football. Empty stays empty.</div>
          </div>
          <span className={discoveryError ? "demo-chip" : "status-badge"}>
            {discoveryError ? "UNAVAILABLE / ERROR" : "LIVE PAPER WHEN CREDENTIALS RESPOND"}
          </span>
        </div>
        <div className="panel-body">
          <p className="section-copy">
            Live triggered: {liveTriggered}. Live near (not arbitrage): {liveNear}.
            {discoveryError ? ` Discovery error: ${discoveryError}` : " Missing credentials fail honestly rather than inventing inventory."}
          </p>
          {liveTriggered === 0 && liveNear === 0 ? (
            <div className="empty-live-compact">
              No live qualifying paper opportunities in the watchlist. Use labelled DEMO / FIXTURE REPLAY below. Fixture rows are not mixed into this live list.
            </div>
          ) : (
            <ul>
              {(snapshot?.live_triggered ?? []).map((item) => (
                <li key={item.opportunity_id}>
                  TRIGGERED {item.home_team} v {item.away_team} · {item.venues.join("/")}
                </li>
              ))}
              {(snapshot?.live_near ?? []).map((item) => (
                <li key={item.opportunity_id}>
                  NEAR {item.home_team} v {item.away_team} · not guaranteed arb
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>

      <div style={{ height: 14 }} />
      <section className="demo-walkthrough">
        <div className="panel-title">3–6. DEMO / FIXTURE REPLAY lifecycle</div>
        <p className="section-copy">
          Labelled fixture path. Same allocator-sized 8F autofill, 8E locks, and unwind/settlement
          close as live paper. Never presented as live venue quotes.
        </p>
        <div className="demo-actions">
          <label className="scan-field">
            Venue pair
            <select value={pair} onChange={(event) => setPair(event.target.value as FixtureReplayResult["venue_pair"])}>
              {PAIRS.map((item) => (
                <option key={item} value={item}>{item.replaceAll("_", " ↔ ")}</option>
              ))}
            </select>
          </label>
          <label className="scan-field">
            Solver
            <select
              value={solver}
              onChange={(event) => setSolver(event.target.value as FixtureReplayResult["solver"])}
            >
              <option value="simple">simple complete-set</option>
              <option value="generalized">generalized payoff (MB↔PM FTTS)</option>
            </select>
          </label>
          <button
            className="demo-primary"
            type="button"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const result = await runFixtureReplay({ venue_pair: pair, solver, close_via: "hold" });
                setReplay(result);
                await refresh();
              })
            }
          >
            Open labelled replay
          </button>
        </div>
        {replay ? (
          <div className="scan-note">
            {replay.label} · {replay.venue_pair} · {replay.solver} · fills {replay.fill_kinds.join(", ") || "—"} ·
            journal {replay.journal_balanced ? "balanced" : "unbalanced"}
          </div>
        ) : null}
      </section>

      <div style={{ height: 14 }} />
      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">4. Active position / capital</div>
            <div className="panel-meta">Native stakes, fill kinds, guaranteed opening economics, free vs locked.</div>
          </div>
          <span className="demo-label">PAPER MODE / EXECUTION DISABLED</span>
        </div>
        <div className="panel-body">
          {!shownTrade ? (
            <div className="empty-live-compact">No paper trade in this demo session yet.</div>
          ) : (
            <>
              <p className="section-copy">
                {shownTrade.fixture_label ?? shownTrade.trade_id} · {shownTrade.state} · solver{" "}
                {shownTrade.solver_model ?? "n/a"} · provenance {shownTrade.provenance}
              </p>
              <div className="scan-note">{fillLine(shownTrade)}</div>
              <div className="hold-grid">
                <div>
                  <div className="metric-label">Guaranteed at open</div>
                  <div className="metric-value" style={{ fontSize: 18 }}>
                    {money(shownTrade.guaranteed_profit_gbp_at_open)}
                  </div>
                  <div className="metric-foot">Recorded only after complete validated hedge + 8E locks</div>
                </div>
                <div>
                  <div className="metric-label">Locked native</div>
                  <div className="metric-value" style={{ fontSize: 18 }}>
                    {Object.keys(shownTrade.capital_locked_native).length
                      ? Object.entries(shownTrade.capital_locked_native)
                          .map(([ccy, amt]) => native(amt, ccy))
                          .join(" · ")
                      : "—"}
                  </div>
                </div>
                <div>
                  <div className="metric-label">Realised P&amp;L</div>
                  <div className="metric-value" style={{ fontSize: 18 }}>{money(shownTrade.realised_pnl_gbp)}</div>
                </div>
              </div>
            </>
          )}
        </div>
      </section>

      <div style={{ height: 14 }} />
      <section className="panel">
        <div className="panel-header">
          <div className="panel-title">5. Hold vs clean unwind</div>
          <span className="status-badge">ANALYTICAL · NOT A RELEASE</span>
        </div>
        <div className="panel-body">
          <HoldVsUnwindCard decision={unwind} />
          {shownTrade?.state === "OPEN" ? (
            <div className="demo-actions" style={{ marginTop: 12 }}>
              <button
                className="demo-primary"
                type="button"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const result = await closeDemoTrade(shownTrade.trade_id, { close_via: "unwind" });
                    setReplay(result);
                    await refresh();
                  })
                }
              >
                Complete validated unwind
              </button>
              <button
                className="pool-reset"
                type="button"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const result = await closeDemoTrade(shownTrade.trade_id, { close_via: "settlement" });
                    setReplay(result);
                    await refresh();
                  })
                }
              >
                Record paper settlement
              </button>
            </div>
          ) : null}
          <p className="section-copy">
            Kalshi SELL close fees are unmodelled, so MB↔Kalshi and PM↔Kalshi unwind fail closed.
            Settlement through 8E is the labelled close path for those pairs. On Matchbook↔Polymarket,
            abundant capital keeps HOLD when reverse-side exit is inferior after fees; unwind posts 8E
            only when 8D says UNWIND_ELIGIBLE. Spread convergence is never a close trigger. Clock
            estimates never release capital.
          </p>
        </div>
      </section>

      <div style={{ height: 14 }} />
      <section className="panel">
        <div className="panel-header">
          <div className="panel-title">6. Close / journal</div>
          <Link className="pool-link" href="/treasury">Treasury board</Link>
        </div>
        <div className="panel-body">
          {replay?.notes.map((note) => (
            <div className="scan-note" key={note}>{note}</div>
          ))}
          {(snapshot?.notes ?? []).map((note) => (
            <div className="scan-note" key={note}>{note}</div>
          ))}
          <p className="section-copy">
            After close, inspect realised betting P&amp;L, fees, released native cash and GBP carrying
            values on Treasury. GBP translation is not native spendable cash.
          </p>
        </div>
      </section>
    </>
  );
}
