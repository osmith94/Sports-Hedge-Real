"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import {
  EconomicsStatus,
  PaperCollectionReport,
  PaperCollectionRequest,
  getEconomicsStatus,
  getLiveRefreshStatus,
  runPaperCollection,
} from "../lib/api";
import { DEFAULT_SCANNER_ASSUMPTIONS } from "../lib/arbitrage-ops";
import { percent } from "../lib/format";

type ScanState =
  | { kind: "idle" }
  | { kind: "success"; report: PaperCollectionReport }
  | { kind: "error"; message: string };

function optionalPositive(value: string, label: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${label} must be greater than zero.`);
  }
  return trimmed;
}

function optionalPercentRate(value: string, label: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed >= 100) {
    throw new Error(`${label} must be between 0% and 100%.`);
  }
  return String(parsed / 100);
}

function reportSummary(report: PaperCollectionReport): string {
  const eligible = report.paper_decisions.filter(
    (decision) => decision.eligible_for_paper_simulation,
  ).length;
  return `${report.matched_event_pairs} event pair${report.matched_event_pairs === 1 ? "" : "s"} · ${report.matched_market_pairs} market pair${report.matched_market_pairs === 1 ? "" : "s"} · ${eligible} paper-eligible · ${report.issues.length} issue${report.issues.length === 1 ? "" : "s"}`;
}

function fxStatusLine(status: EconomicsStatus | null): string {
  if (!status) return "FX backend-resolved · loading";
  const usd = status.fx.find((row) => row.currency === "USD");
  if (!usd) {
    const missing = status.issues.find((issue) => issue.includes("missing_fx_rate")) || "missing USD rate";
    return `FX USD/GBP unavailable · ${missing}`;
  }
  const sourceDate = usd.source_date.slice(0, 10);
  const source = usd.primary_source.includes("ecb") ? "ECB" : usd.primary_source;
  return `FX USD/GBP ${usd.gbp_per_unit} · ${source} ${sourceDate} · ${usd.status}`;
}

function costStatusLine(status: EconomicsStatus | null): string {
  if (!status) return "Venue costs backend-resolved · loading";
  const matchbook = status.venue_costs.find((row) => row.venue === "matchbook");
  const polymarket = status.venue_costs.find((row) => row.venue === "polymarket");
  if (!matchbook || !polymarket) return "Venue costs incomplete · fail closed if required rule missing";
  const mb = matchbook.rate ? `${(Number(matchbook.rate) * 100).toFixed(2)}% ${matchbook.fee_basis}` : matchbook.fee_basis;
  return `Costs Matchbook ${mb} · Polymarket ${polymarket.fee_basis} · ${matchbook.source}`;
}

export function RunPaperScan() {
  const router = useRouter();
  const [capitalLimit, setCapitalLimit] = useState("");
  const [minNetArbPercent, setMinNetArbPercent] = useState(
    String(DEFAULT_SCANNER_ASSUMPTIONS.minimumNetArb * 100),
  );
  const [maxRisk, setMaxRisk] = useState(String(DEFAULT_SCANNER_ASSUMPTIONS.maximumExecutionRisk));
  const [loading, setLoading] = useState(false);
  const [state, setState] = useState<ScanState>({ kind: "idle" });
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [intervalSeconds, setIntervalSeconds] = useState(30);
  const [economics, setEconomics] = useState<EconomicsStatus | null>(null);
  const payloadRef = useRef<PaperCollectionRequest>({ maximum_execution_risk: 60 });
  const inFlightRef = useRef(false);
  const collectRef = useRef<() => Promise<void>>(async () => undefined);

  const buildPayload = useCallback((): PaperCollectionRequest => {
    const capital = optionalPositive(capitalLimit, "Capital limit");
    const minNet = optionalPercentRate(minNetArbPercent, "Minimum net arb");
    const risk = Number(maxRisk);
    if (!Number.isInteger(risk) || risk < 0 || risk > 100) {
      throw new Error("Maximum execution risk must be a whole number from 0 to 100.");
    }
    const payload: PaperCollectionRequest = {
      maximum_execution_risk: risk,
    };
    if (capital) payload.capital_limit_gbp = capital;
    if (minNet) payload.minimum_net_edge = minNet;
    return payload;
  }, [capitalLimit, maxRisk, minNetArbPercent]);

  useEffect(() => {
    try {
      payloadRef.current = buildPayload();
    } catch {
      payloadRef.current = { maximum_execution_risk: 60 };
    }
  }, [buildPayload]);

  const refreshEconomics = useCallback(async () => {
    try {
      setEconomics(await getEconomicsStatus());
    } catch {
      setEconomics(null);
    }
  }, []);

  const collect = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setLoading(true);
    setState({ kind: "idle" });
    try {
      const payload = buildPayload();
      payloadRef.current = payload;
      const report = await runPaperCollection(payload);
      setState({ kind: "success", report });
      await refreshEconomics();
      router.refresh();
    } catch (error) {
      setState({
        kind: "error",
        message: error instanceof Error ? error.message : "Read-only scan failed.",
      });
    } finally {
      inFlightRef.current = false;
      setLoading(false);
    }
  }, [buildPayload, refreshEconomics, router]);

  useEffect(() => {
    collectRef.current = collect;
  }, [collect]);

  useEffect(() => {
    void refreshEconomics();
  }, [refreshEconomics]);

  useEffect(() => {
    let cancelled = false;
    getLiveRefreshStatus()
      .then((status) => {
        if (!cancelled && status.interval_seconds) {
          setIntervalSeconds(status.interval_seconds);
        }
      })
      .catch(() => {
        // Status endpoint down: keep the 30s default cadence.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!autoRefresh) return undefined;
    const cadenceMs = Math.max(15, intervalSeconds) * 1000;
    const timer = window.setInterval(() => {
      void collectRef.current();
    }, cadenceMs);
    return () => window.clearInterval(timer);
  }, [autoRefresh, intervalSeconds]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await collect();
  }

  const triggerDisplay = minNetArbPercent.trim()
    ? `${minNetArbPercent.trim()}%`
    : percent(DEFAULT_SCANNER_ASSUMPTIONS.minimumNetArb);

  return (
    <section className="panel scan-control">
      <div className="panel-header">
        <div>
          <div className="panel-title">Paper scanner controls</div>
          <div className="panel-meta">
            Matchbook discovers fixtures; Polymarket is matched onto the same canonical event. Repeated
            read-only collection updates watchlist net margin and distance-to-strike. PAPER MODE · no
            orders.
          </div>
        </div>
        <span className="status-badge">PAPER MODE · NO EXECUTION</span>
      </div>

      <form className="scan-form" onSubmit={submit}>
        <div className="assumption-strip">
          <span>Trigger {triggerDisplay} net arb</span>
          <span>Capital {capitalLimit.trim() ? `£${capitalLimit.trim()}` : "unset"}</span>
          <span>Max risk {maxRisk}/100</span>
          <span>{fxStatusLine(economics)}</span>
          <span>{costStatusLine(economics)}</span>
          <span>Cadence {intervalSeconds}s</span>
        </div>

        <div className="scan-control-grid scan-control-grid-ops">
          <label className="scan-field">
            <span>Min net arb %</span>
            <input
              inputMode="decimal"
              value={minNetArbPercent}
              onChange={(event) => setMinNetArbPercent(event.target.value)}
              placeholder="1.00"
              aria-label="Minimum net arbitrage trigger percent"
            />
          </label>
          <label className="scan-field">
            <span>Capital limit £</span>
            <input
              inputMode="decimal"
              value={capitalLimit}
              onChange={(event) => setCapitalLimit(event.target.value)}
              placeholder="optional"
              aria-label="Paper capital limit pounds"
            />
          </label>
          <label className="scan-field">
            <span>Max risk / 100</span>
            <input
              inputMode="numeric"
              value={maxRisk}
              onChange={(event) => setMaxRisk(event.target.value)}
              aria-label="Maximum execution risk score"
            />
          </label>
          <div className="scan-action">
            <button className="scan-button" type="submit" disabled={loading}>
              {loading ? "Scanning…" : "Run read-only scan"}
            </button>
          </div>
        </div>

        <label className="scan-note">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(event) => setAutoRefresh(event.target.checked)}
          />{" "}
          Keep refreshing while this console is open (read-only Matchbook/Polymarket collection at{" "}
          {intervalSeconds}s). Does not place orders. Server loop stays off unless{" "}
          <code>PAPER_LIVE_REFRESH_ENABLED</code> is set.
        </label>

        <div className="scan-note">
          FX and venue fees are backend-resolved with timestamped provenance. The operator console
          cannot enter USD→GBP or venue-fee percentages for the solver. Missing or stale required
          inputs fail closed. {economics?.data_kind === "backend_resolved" ? "Data: backend_resolved." : ""}
        </div>

        {state.kind === "success" ? (
          <div className="scan-message scan-message-success" role="status">
            {reportSummary(state.report)}
            {state.report.issues.length > 0 ? (
              <span> · First issue: {state.report.issues[0].detail}</span>
            ) : null}
          </div>
        ) : null}

        {state.kind === "error" ? (
          <div className="scan-message scan-message-error" role="alert">
            {state.message}
          </div>
        ) : null}
      </form>
    </section>
  );
}
