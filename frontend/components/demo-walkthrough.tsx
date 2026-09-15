"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { HoldVsUnwindCard } from "./hold-vs-unwind";
import { PaperDeploymentPreview } from "./paper-deployment-preview";
import {
  DemoWalkthroughSnapshot,
  FixtureReplayResult,
  PaperTrade,
  closeDemoTrade,
  getDemoWalkthrough,
  getLiveRefreshStatus,
  resetDemoWalkthrough,
  runFixtureReplay,
  runPaperCollection,
} from "../lib/api";
import { DEFAULT_SCANNER_ASSUMPTIONS } from "../lib/arbitrage-ops";
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

function pairSupportsGeneralized(pair: FixtureReplayResult["venue_pair"]): boolean {
  return pair === "matchbook_polymarket";
}

export function DemoWalkthroughBoard() {
  const [snapshot, setSnapshot] = useState<DemoWalkthroughSnapshot | null>(null);
  const [replay, setReplay] = useState<FixtureReplayResult | null>(null);
  const [pair, setPair] = useState<FixtureReplayResult["venue_pair"]>("matchbook_polymarket");
  const [solver, setSolver] = useState<FixtureReplayResult["solver"]>("simple");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [available, setAvailable] = useState(true);
  const [discoveryBusy, setDiscoveryBusy] = useState(false);
  const [discoveryMessage, setDiscoveryMessage] = useState<string | null>(null);
  const [autoLiveRefresh, setAutoLiveRefresh] = useState(true);
  const [intervalSeconds, setIntervalSeconds] = useState(30);
  const [serverOwned, setServerOwned] = useState(false);
  const collectInFlight = useRef(false);
  const collectRef = useRef<() => Promise<void>>(async () => undefined);
  const serverOwnedRef = useRef(false);

  const refresh = useCallback(async () => {
    const next = await getDemoWalkthrough();
    setSnapshot(next);
    setReplay(next.replay ?? null);
    setAvailable(true);
  }, []);

  const refreshLiveDiscovery = useCallback(async () => {
    let owned = serverOwnedRef.current;
    try {
      const status = await getLiveRefreshStatus();
      owned = Boolean(status.server_loop_enabled);
      serverOwnedRef.current = owned;
      setServerOwned(owned);
      if (status.interval_seconds) setIntervalSeconds(status.interval_seconds);
    } catch {
      owned = serverOwnedRef.current;
    }
    if (owned) {
      try {
        await refresh();
        setDiscoveryMessage(null);
      } catch (err) {
        setDiscoveryMessage(
          err instanceof Error ? err.message : "Live discovery UNAVAILABLE",
        );
      }
      return;
    }
    if (collectInFlight.current) return;
    collectInFlight.current = true;
    setDiscoveryBusy(true);
    try {
      await runPaperCollection({
        maximum_execution_risk: DEFAULT_SCANNER_ASSUMPTIONS.maximumExecutionRisk,
      });
      setDiscoveryMessage(null);
    } catch (err) {
      setDiscoveryMessage(
        err instanceof Error ? err.message : "Live discovery UNAVAILABLE",
      );
    } finally {
      collectInFlight.current = false;
      setDiscoveryBusy(false);
      try {
        await refresh();
      } catch (err) {
        setAvailable(false);
        setError(err instanceof Error ? err.message : "Demo walkthrough API unavailable");
      }
    }
  }, [refresh]);

  useEffect(() => {
    collectRef.current = refreshLiveDiscovery;
  }, [refreshLiveDiscovery]);

  useEffect(() => {
    refresh()
      .then(() => collectRef.current())
      .catch((err: unknown) => {
        setAvailable(false);
        setError(err instanceof Error ? err.message : "Demo walkthrough API unavailable");
      });
  }, [refresh]);

  useEffect(() => {
    let cancelled = false;
    getLiveRefreshStatus()
      .then((status) => {
        if (!cancelled && status.interval_seconds) {
          setIntervalSeconds(status.interval_seconds);
        }
        if (!cancelled) {
          const owned = Boolean(status.server_loop_enabled);
          serverOwnedRef.current = owned;
          setServerOwned(owned);
        }
      })
      .catch(() => {
        // Status endpoint down: keep the 30s default cadence used by the operations console.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!autoLiveRefresh) return undefined;
    const cadenceMs = Math.max(15, intervalSeconds) * 1000;
    const timer = window.setInterval(() => {
      void collectRef.current();
    }, cadenceMs);
    return () => window.clearInterval(timer);
  }, [autoLiveRefresh, intervalSeconds]);

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

  const effectiveSolver: FixtureReplayResult["solver"] = pairSupportsGeneralized(pair)
    ? solver
    : "simple";
  const liveTriggered = snapshot?.live_triggered.length ?? 0;
  const liveNear = snapshot?.live_near.length ?? 0;
  const discovery = snapshot?.discovery;
  const discoveryError = discoveryMessage ?? discovery?.last_error ?? null;
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
            fixture rows, then qualify a labelled replay and confirm an operator-chosen size
            (example £10). Live auto-capture on `/` does not apply here. Phase 1 remains
            PAPER MODE. execution_enabled=false.
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
                  await collectRef.current();
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
                  await collectRef.current();
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
            <div className="panel-meta">
              Read-only live/near-future football via the broad `/paper/collect` diagnostic path.
              The operations console primary Run scan uses bounded HOT instead. Empty stays empty.
              Fixture replay is never mixed into these rows.
            </div>
          </div>
          <span className={discoveryError ? "demo-chip" : "status-badge"}>
            {discoveryError ? "UNAVAILABLE / ERROR" : "LIVE PAPER WHEN CREDENTIALS RESPOND"}
          </span>
        </div>
        <div className="panel-body">
          <div className="demo-actions">
            <button
              className="demo-primary"
              type="button"
              disabled={busy || discoveryBusy}
              onClick={() => void refreshLiveDiscovery()}
            >
              {discoveryBusy ? "Refreshing live discovery…" : "Refresh Live Discovery"}
            </button>
            <label className="scan-refresh">
              <input
                type="checkbox"
                checked={autoLiveRefresh}
                onChange={(event) => setAutoLiveRefresh(event.target.checked)}
              />
              Auto {intervalSeconds}s{serverOwned ? " view" : ""}
            </label>
          </div>
          <p className="section-copy">
            Live triggered: {liveTriggered}. Live near (not arbitrage): {liveNear}.
            {discovery?.last_matched_event_pairs != null
              ? ` Last collection: ${discovery.last_matched_event_pairs} event pair(s), ${discovery.last_matched_market_pairs ?? 0} market pair(s).`
              : " Collection has not completed yet."}
            {discovery?.server_loop_enabled ? " Server live-refresh loop is enabled for this process." : ""}
            {discoveryError
              ? ` Discovery error: ${discoveryError}`
              : " Missing credentials fail honestly rather than inventing inventory."}
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
          Labelled fixture path. Qualify first (no OPEN, no lock). Then preview exact native legs
          and confirm the accepted £10. Confirm revalidates current economics; it does not place
          venue orders. Never presented as live venue quotes.
        </p>
        <div className="demo-actions">
          <label className="scan-field">
            Venue pair
            <select
              value={pair}
              onChange={(event) => {
                const next = event.target.value as FixtureReplayResult["venue_pair"];
                setPair(next);
                if (!pairSupportsGeneralized(next)) {
                  setSolver("simple");
                }
              }}
            >
              {PAIRS.map((item) => (
                <option key={item} value={item}>{item.replaceAll("_", " ↔ ")}</option>
              ))}
            </select>
          </label>
          <label className="scan-field">
            Solver
            <select
              value={effectiveSolver}
              onChange={(event) => setSolver(event.target.value as FixtureReplayResult["solver"])}
            >
              <option value="simple">simple complete-set</option>
              <option value="generalized" disabled={!pairSupportsGeneralized(pair)}>
                generalized payoff (MB↔PM FTTS only)
              </option>
            </select>
          </label>
          <button
            className="demo-primary"
            type="button"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const result = await runFixtureReplay({
                  venue_pair: pair,
                  solver: effectiveSolver,
                  close_via: "hold",
                  qualify_only: true,
                });
                setReplay(result);
                await refresh();
              })
            }
          >
            Qualify labelled replay
          </button>
          <button
            className="pool-reset"
            type="button"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                const result = await runFixtureReplay({
                  venue_pair: pair,
                  solver: effectiveSolver,
                  close_via: "hold",
                });
                setReplay(result);
                await refresh();
              })
            }
          >
            Allocator-sized autofill OPEN
          </button>
        </div>
        {!pairSupportsGeneralized(pair) ? (
          <div className="scan-note">
            Generalized replay is Matchbook↔Polymarket first-team-to-score only. This pair uses the
            simple complete-set fixture.
          </div>
        ) : null}
        {replay ? (
          <div className="scan-note">
            {replay.label} · {replay.venue_pair} · {replay.solver} · fills {replay.fill_kinds.join(", ") || "—"} ·
            journal {replay.journal_balanced ? "balanced" : "unbalanced"}
            {replay.qualify_only ? " · qualified only (no OPEN yet)" : ""}
          </div>
        ) : null}
        {replay?.qualify_only && (replay.preparable_opportunities?.length ?? 0) > 0 ? (
          <PaperDeploymentPreview
            opportunities={replay.preparable_opportunities ?? []}
            provenance="fixture_demo"
            onOpened={() => {
              void refresh();
            }}
          />
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
          {shownTrade?.state === "OPEN" || shownTrade?.state === "CLOSED" ? (
            <div className="demo-actions" style={{ marginTop: 12 }}>
              {shownTrade.state === "OPEN" ? (
              <button
                className="pool-reset"
                type="button"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await refresh();
                  })
                }
              >
                Hold — do not release
              </button>
              ) : null}
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
            Hold vs unwind uses the stored labelled reverse book (HOLD when exit is inferior).
            Complete validated unwind uses a labelled tighter reverse book for the DEMO / FIXTURE REPLAY
            close proof; not a live touch. Spread convergence is never a close trigger. Clock
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
            values on Treasury. GBP translation is not native spendable cash. Paper full-fill success
            models latency/slippage/depth/partials; it does not prove simultaneous real fills.
          </p>
        </div>
      </section>
    </>
  );
}
