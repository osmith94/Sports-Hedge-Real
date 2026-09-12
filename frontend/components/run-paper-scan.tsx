"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import {
  EconomicsStatus,
  EconomicsVenueCostRow,
  PaperCollectionReport,
  PaperCollectionRequest,
  getEconomicsStatus,
  getLiveRefreshStatus,
  runPaperCollection,
} from "../lib/api";
import { DEFAULT_SCANNER_ASSUMPTIONS } from "../lib/arbitrage-ops";

type ScanState =
  | { kind: "idle" }
  | { kind: "success"; report: PaperCollectionReport }
  | { kind: "error"; message: string };

type StripChip = {
  key: string;
  label: string;
  warn: boolean;
  title: string;
};

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

function sourceLabel(source: string): string {
  if (source.includes("ecb")) return "ECB";
  if (source.includes("boe")) return "BoE";
  if (source.includes("matchbook")) return "registry";
  if (source.includes("polymarket")) return "registry";
  return source.replace(/^venue_cost_registry:/, "");
}

function clockStamp(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const parsed = Date.parse(iso);
  if (!Number.isFinite(parsed)) return iso.slice(0, 16);
  return `${new Date(parsed).toISOString().replace("T", " ").slice(0, 16)}Z`;
}

function venueCostChip(
  status: EconomicsStatus | null,
  venue: "matchbook" | "polymarket",
  short: string,
): StripChip {
  const preferred = ["match_result", "both_teams_to_score"];
  const rows = status?.venue_costs.filter((row) => row.venue === venue) ?? [];
  const row =
    preferred.map((family) => rows.find((item) => item.market_class === family)).find(Boolean) ??
    rows[0];
  if (!status) {
    return { key: venue, label: `${short} …`, warn: false, title: "Loading backend venue costs" };
  }
  if (!row) {
    const diagnostic = status.issues.find((item) => item.includes(venue)) ?? `${venue} cost missing`;
    return { key: venue, label: `${short} warn`, warn: true, title: diagnostic };
  }
  return {
    key: venue,
    label: `${short} ${formatFeeRule(row)}`,
    warn: row.known_status !== "known",
    title: [
      `${row.fee_basis} · ${row.action}`,
      row.market_class,
      row.source,
      row.effective_from ? `effective ${row.effective_from.slice(0, 10)}` : null,
      row.snapshot_id,
      row.detail,
    ]
      .filter(Boolean)
      .join(" · "),
  };
}

function formatFeeRule(row: EconomicsVenueCostRow): string {
  if (row.rate != null && row.rate !== "" && Number(row.rate) > 0) {
    return `${(Number(row.rate) * 100).toFixed(2)}% ${row.fee_basis}`;
  }
  return row.fee_basis.replaceAll("_", " ");
}

function economicsChips(status: EconomicsStatus | null): StripChip[] {
  const usd = status?.fx.find((row) => row.currency === "USD");
  const fxIssue = status?.issues.find((item) => item.includes("fx_rate")) ?? "missing USD rate";
  const fxChip: StripChip = usd
    ? {
        key: "fx",
        label: `USD/GBP ${Number(usd.gbp_per_unit).toFixed(4)} · ${sourceLabel(usd.primary_source)} ${usd.source_date.slice(0, 10)} · ${usd.status}`,
        warn:
          usd.status === "exception" ||
          Boolean(status?.issues.some((item) => item.includes("stale_fx"))),
        title: [
          `primary ${usd.primary_source}`,
          `published ${usd.source_date}`,
          usd.retrieved_at ? `retrieved ${clockStamp(usd.retrieved_at)}` : null,
          `valuation ${usd.valuation_date}`,
          usd.check_source ? `check ${usd.check_source}` : null,
          usd.variance_bps ? `variance ${usd.variance_bps} bps` : null,
          status?.data_kind,
        ]
          .filter(Boolean)
          .join(" · "),
      }
    : {
        key: "fx",
        label: status ? "USD/GBP warn" : "USD/GBP …",
        warn: Boolean(status),
        title: status ? fxIssue : "Loading backend FX",
      };

  return [
    fxChip,
    venueCostChip(status, "matchbook", "MB"),
    venueCostChip(status, "polymarket", "PM"),
  ];
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

  const chips = economicsChips(economics);

  return (
    <section className="panel scan-control">
      <div className="panel-header">
        <div>
          <div className="panel-title">Paper scanner</div>
          <div className="panel-meta">Read-only collection. Stake sizing uses standing native venue pools.</div>
        </div>
        <span className="status-badge">PAPER MODE · NO EXECUTION</span>
      </div>

      <form className="scan-form" onSubmit={submit}>
        <div className="scan-ops-row">
          <label className="scan-field scan-field-primary">
            <span>Min net arb %</span>
            <input
              inputMode="decimal"
              value={minNetArbPercent}
              onChange={(event) => setMinNetArbPercent(event.target.value)}
              placeholder="0.50"
              aria-label="Minimum net arbitrage trigger percent"
            />
          </label>
          <label className="scan-field scan-field-compact">
            <span>Max risk</span>
            <input
              inputMode="numeric"
              value={maxRisk}
              onChange={(event) => setMaxRisk(event.target.value)}
              aria-label="Maximum execution risk score"
            />
          </label>
          <label className="scan-refresh">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(event) => setAutoRefresh(event.target.checked)}
            />
            Auto {intervalSeconds}s
          </label>
          <div className="scan-action">
            <button className="scan-button" type="submit" disabled={loading}>
              {loading ? "Scanning…" : "Run scan"}
            </button>
          </div>
        </div>

        <div className="econ-strip" aria-label="Backend-resolved FX and venue costs">
          {chips.map((chip) => (
            <span
              key={chip.key}
              className={chip.warn ? "econ-chip econ-chip-warn" : "econ-chip"}
              title={chip.title}
            >
              {chip.label}
            </span>
          ))}
        </div>

        <details className="scan-advanced">
          <summary>Advanced · provenance</summary>
          <p className="scan-advanced-copy">
            FX and venue fees are backend-resolved ({economics?.data_kind ?? "backend_resolved"}).
            Missing or stale required inputs fail closed. Standing capital is Matchbook GBP /
            Polymarket USD / Smarkets GBP — never a combined cash figure. Optional extra
            capital_limit_gbp is a scan-only cap, not live funds.
          </p>
          <label className="scan-field">
            <span>Optional capital limit £</span>
            <input
              inputMode="decimal"
              value={capitalLimit}
              onChange={(event) => setCapitalLimit(event.target.value)}
              placeholder="native pools"
              aria-label="Optional paper capital limit pounds"
            />
          </label>
        </details>

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
