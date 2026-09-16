"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import {
  EconomicsStatus,
  EconomicsVenueCostRow,
  LiveRefreshStatus,
  PaperCollectionReport,
  PaperCollectionRequest,
  getEconomicsStatus,
  getLiveRefreshStatus,
  resetMatchbookFee,
  runPaperCollection,
  runPaperHotRefresh,
  saveMatchbookFee,
} from "../lib/api";
import { DEFAULT_SCANNER_ASSUMPTIONS } from "../lib/arbitrage-ops";
import { dualScanStatusLines } from "../lib/scan-status-display";
import { CONFIG_WARNING_BANNER_CLASS } from "../lib/config-warning-display";
import { applyLatestLiveRefresh, createLiveRefreshPollGuard } from "../lib/live-refresh-poll-guard";
import { venueHealthIsDegraded } from "../lib/venue-health-display";
import { LiveScanPulse, LiveScanPulsePhase } from "./live-scan-pulse";
import { VenueLaneControls } from "./venue-lane-controls";

type ScanState =
  | { kind: "idle" }
  | { kind: "success"; report: PaperCollectionReport }
  | { kind: "error"; message: string };

type ScanMode = "hot" | "diagnostic";

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
  if (report.operator_summary) return report.operator_summary;
  const eligible = report.paper_decisions.filter(
    (decision) => decision.eligible_for_paper_simulation,
  ).length;
  return `${report.matched_event_pairs} event pair${report.matched_event_pairs === 1 ? "" : "s"} · ${report.matched_market_pairs} market pair${report.matched_market_pairs === 1 ? "" : "s"} · ${eligible} paper-eligible · ${report.issues.length} genuine issue${report.issues.length === 1 ? "" : "s"}`;
}

function clampIntervalSeconds(value: number): number {
  if (!Number.isFinite(value)) return 30;
  return Math.min(300, Math.max(15, Math.round(value)));
}

function sourceLabel(source: string): string {
  if (source.includes("ecb")) return "ECB";
  if (source.includes("boe")) return "BoE";
  if (source.includes("matchbook")) return "registry";
  if (source.includes("polymarket")) return "registry";
  if (source.includes("kalshi")) return "series";
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
  venue: "matchbook" | "polymarket" | "kalshi",
  short: string,
): StripChip {
  if (venue === "matchbook" && status?.matchbook_fee) {
    const row = status.matchbook_fee;
    return {
      key: venue,
      label: `${short} ${row.label}`,
      warn: false,
      title: [
        row.fee_basis,
        row.source,
        row.account_assumption ? "operator/account assumption" : "provider default",
        row.detail,
      ]
        .filter(Boolean)
        .join(" · "),
    };
  }
  if (venue === "polymarket" && status?.polymarket_fee_policy) {
    const policy = status.polymarket_fee_policy;
    return {
      key: venue,
      label: `${short} per-market CLOB metadata`,
      warn: false,
      title: `${policy.resolution} · ${policy.detail}`,
    };
  }
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
          usd.carried_forward ? "carried forward" : null,
          usd.check_status ? `BoE ${usd.check_status}` : null,
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
    venueCostChip(status, "kalshi", "KS"),
  ];
}

export function RunPaperScan() {
  const router = useRouter();
  const [capitalLimit, setCapitalLimit] = useState("");
  const [minNetArbPercent, setMinNetArbPercent] = useState(
    String(DEFAULT_SCANNER_ASSUMPTIONS.minimumNetArb * 100),
  );
  const [maxRisk, setMaxRisk] = useState(String(DEFAULT_SCANNER_ASSUMPTIONS.maximumExecutionRisk));
  const [loadingMode, setLoadingMode] = useState<ScanMode | null>(null);
  const [state, setState] = useState<ScanState>({ kind: "idle" });
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [intervalSeconds, setIntervalSeconds] = useState(30);
  const [intervalDraft, setIntervalDraft] = useState("30");
  const [lastCompletedAt, setLastCompletedAt] = useState<string | null>(null);
  const [lastDurationMs, setLastDurationMs] = useState<number | null>(null);
  const [liveRefresh, setLiveRefresh] = useState<LiveRefreshStatus | null>(null);
  const [venueHealth, setVenueHealth] = useState<Record<string, string> | null>(null);
  const [completeFlash, setCompleteFlash] = useState(false);
  const [nowMs, setNowMs] = useState<number | null>(null);
  const [autoAnchorMs, setAutoAnchorMs] = useState<number | null>(null);
  const [economics, setEconomics] = useState<EconomicsStatus | null>(null);
  const [matchbookCommissionPercent, setMatchbookCommissionPercent] = useState("2.00");
  const [feeSaving, setFeeSaving] = useState(false);
  const [feeMessage, setFeeMessage] = useState<string | null>(null);
  const payloadRef = useRef<PaperCollectionRequest>({ maximum_execution_risk: 60 });
  const inFlightRef = useRef(false);
  const lastSeenCycleRef = useRef<string>("");
  const liveRefreshPollGuardRef = useRef(createLiveRefreshPollGuard());

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

  const applyLiveRefresh = useCallback(
    (status: LiveRefreshStatus) => {
      if (status.interval_seconds) {
        const clamped = clampIntervalSeconds(status.interval_seconds);
        setIntervalSeconds(clamped);
        setIntervalDraft(String(clamped));
      }
      const completed =
        status.hot?.last_completed_at ??
        status.universe?.last_completed_at ??
        status.last_completed_at ??
        null;
      if (completed) setLastCompletedAt(completed);
      const duration =
        status.hot?.last_duration_ms ??
        status.universe?.last_duration_ms ??
        status.last_duration_ms ??
        null;
      if (duration != null) setLastDurationMs(duration);
      if (status.venue_health) setVenueHealth(status.venue_health);
      setLiveRefresh(status);
      const stamp = `${status.hot?.last_completed_at ?? ""}|${status.universe?.last_completed_at ?? ""}`;
      if (stamp !== lastSeenCycleRef.current) {
        lastSeenCycleRef.current = stamp;
        if (stamp !== "|") router.refresh();
      }
    },
    [router],
  );

  const collect = useCallback(async (mode: ScanMode) => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    liveRefreshPollGuardRef.current.begin();
    setLoadingMode(mode);
    setState({ kind: "idle" });
    try {
      const payload = buildPayload();
      payloadRef.current = payload;
      const report =
        mode === "hot"
          ? await runPaperHotRefresh(payload)
          : await runPaperCollection(payload);
      setState({ kind: "success", report });
      setLastCompletedAt(report.completed_at);
      setVenueHealth(report.venue_health ?? null);
      setCompleteFlash(true);
      setNowMs(Date.now());
      const started = Date.parse(report.started_at);
      const completed = Date.parse(report.completed_at);
      if (Number.isFinite(started) && Number.isFinite(completed)) {
        setLastDurationMs(Math.max(0, completed - started));
      }
      try {
        await applyLatestLiveRefresh(
          liveRefreshPollGuardRef.current,
          getLiveRefreshStatus,
          applyLiveRefresh,
        );
      } catch {
        // Keep the completed report facts if status is briefly unavailable.
      }
      await refreshEconomics();
    } catch (error) {
      setState({
        kind: "error",
        message: error instanceof Error ? error.message : "Read-only scan failed.",
      });
      try {
        await applyLatestLiveRefresh(
          liveRefreshPollGuardRef.current,
          getLiveRefreshStatus,
          applyLiveRefresh,
        );
      } catch {
        // Keep prior last-scan facts. A failed collect is not a completed scan.
      }
    } finally {
      inFlightRef.current = false;
      setLoadingMode(null);
      router.refresh();
    }
  }, [applyLiveRefresh, buildPayload, refreshEconomics, router]);

  const pollLiveStatus = useCallback(async () => {
    try {
      await applyLatestLiveRefresh(
        liveRefreshPollGuardRef.current,
        getLiveRefreshStatus,
        applyLiveRefresh,
      );
    } catch {
      // Status endpoint down: keep prior Fast/Full facts.
    }
  }, [applyLiveRefresh]);

  useEffect(() => {
    void refreshEconomics();
  }, [refreshEconomics]);

  useEffect(() => {
    const rate = economics?.matchbook_fee?.effective_rate;
    if (rate == null || rate === "") return;
    const parsed = Number(rate) * 100;
    if (!Number.isFinite(parsed)) return;
    setMatchbookCommissionPercent(parsed.toFixed(2));
  }, [economics?.matchbook_fee?.effective_rate]);

  useEffect(() => {
    let cancelled = false;
    void applyLatestLiveRefresh(
      liveRefreshPollGuardRef.current,
      getLiveRefreshStatus,
      (status) => {
        if (cancelled) return;
        applyLiveRefresh(status);
      },
    ).catch(() => {
      // Status endpoint down: keep the 30s default cadence.
    });
    return () => {
      cancelled = true;
    };
  }, [applyLiveRefresh]);

  useEffect(() => {
    if (!completeFlash) return undefined;
    const timer = window.setTimeout(() => setCompleteFlash(false), 550);
    return () => window.clearTimeout(timer);
  }, [completeFlash]);

  useEffect(() => {
    if (!autoRefresh) {
      setAutoAnchorMs(null);
      return undefined;
    }
    const serverOwned = Boolean(liveRefresh?.server_loop_enabled);
    const cadenceMs = serverOwned ? 2000 : clampIntervalSeconds(intervalSeconds) * 1000;
    setAutoAnchorMs(Date.now());
    void pollLiveStatus();
    const timer = window.setInterval(() => {
      void pollLiveStatus();
    }, cadenceMs);
    return () => window.clearInterval(timer);
  }, [autoRefresh, intervalSeconds, liveRefresh?.server_loop_enabled, pollLiveStatus]);

  useEffect(() => {
    if (!autoRefresh || loadingMode !== null) return undefined;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadingMode]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await collect("hot");
  }

  async function saveMatchbookCommission() {
    const parsed = Number(matchbookCommissionPercent);
    if (!Number.isFinite(parsed) || parsed < 0 || parsed >= 100) {
      setFeeMessage("Matchbook commission must be between 0% and 100%.");
      return;
    }
    setFeeSaving(true);
    setFeeMessage(null);
    try {
      await saveMatchbookFee((parsed / 100).toFixed(6).replace(/\.?0+$/, "") || "0");
      await refreshEconomics();
      setFeeMessage("Saved operator/account Matchbook commission.");
    } catch (error) {
      setFeeMessage(error instanceof Error ? error.message : "Could not save Matchbook commission.");
    } finally {
      setFeeSaving(false);
    }
  }

  async function resetMatchbookCommission() {
    setFeeSaving(true);
    setFeeMessage(null);
    try {
      await resetMatchbookFee();
      await refreshEconomics();
      setFeeMessage("Reset to UK Matchbook provider default 2.00%.");
    } catch (error) {
      setFeeMessage(error instanceof Error ? error.message : "Could not reset Matchbook commission.");
    } finally {
      setFeeSaving(false);
    }
  }

  const chips = economicsChips(economics);
  const loading = loadingMode !== null;
  const cadenceMs = clampIntervalSeconds(intervalSeconds) * 1000;
  const serverOwned = Boolean(liveRefresh?.server_loop_enabled);
  const nextHotMs = liveRefresh?.hot?.next_due_at
    ? Date.parse(liveRefresh.hot.next_due_at)
    : Number.NaN;
  const nextRefreshSeconds = !autoRefresh || nowMs == null
    ? null
    : Number.isFinite(nextHotMs)
      ? Math.max(0, Math.ceil((nextHotMs - nowMs) / 1000))
      : autoAnchorMs != null
        ? Math.max(0, Math.ceil((autoAnchorMs + cadenceMs - nowMs) / 1000))
        : null;
  const pulsePhase: LiveScanPulsePhase = loading
    ? "scanning"
    : state.kind === "error"
      ? "error"
      : venueHealthIsDegraded(venueHealth ?? undefined)
        ? "degraded"
        : completeFlash
          ? "complete"
          : !autoRefresh
            ? "paused"
            : "idle";

  return (
    <section className="panel scan-control">
      <div className="panel-header">
        <div>
          <div className="panel-title">Paper scanner</div>
          <div className="panel-meta">
            Server-owned Fast/Full scanner plus manual HOT refresh and bounded diagnostics.
          </div>
        </div>
        <div className="heading-actions">
          <span className="status-badge">PAPER MODE · NO EXECUTION</span>
          {liveRefresh?.paper_autofill_enabled ? (
            <span className="status-badge">AUTO PAPER CAPTURE ON</span>
          ) : null}
        </div>
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
          <label className="scan-field scan-field-compact">
            <span>Refresh interval s</span>
            <input
              inputMode="numeric"
              value={intervalDraft}
              onChange={(event) => setIntervalDraft(event.target.value)}
              onBlur={() => {
                const clamped = clampIntervalSeconds(Number(intervalDraft));
                setIntervalSeconds(clamped);
                setIntervalDraft(String(clamped));
              }}
              aria-label="Refresh interval seconds"
              title="Safe range 15–300 seconds"
            />
          </label>
          <label className="scan-refresh">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(event) => setAutoRefresh(event.target.checked)}
              aria-label={
                serverOwned
                  ? "Auto refresh Fast scan and Full sweep status"
                  : "Auto refresh"
              }
            />
            {serverOwned ? "Auto refresh view" : "Auto refresh"}
          </label>
          <LiveScanPulse
            phase={pulsePhase}
            nextRefreshSeconds={nextRefreshSeconds}
            venueHealth={venueHealth}
            errorMessage={state.kind === "error" ? state.message : null}
          />
          <div className="scan-action">
            <button className="scan-button" type="submit" disabled={loading} aria-busy={loading}>
              {loadingMode === "hot" ? "Scanning… Refreshing HOT…" : "Run scan"}
            </button>
          </div>
        </div>
        <div className="scan-note">
          Run scan performs a manual HOT refresh of current known fixtures.
          It does not rediscover the full universe or advance the scheduled Fast Scan / Full Sweep
          lanes.
        </div>
        <div className="scan-note" aria-label="Fast scan and Full sweep status">
          {dualScanStatusLines(liveRefresh, nowMs).map((line) => (
            <div key={line}>{line}</div>
          ))}
          {autoRefresh
            ? serverOwned
              ? " · auto on · view refresh · server owns Fast/Full scans"
              : " · auto on"
            : " · auto off"}
        </div>
        <VenueLaneControls
          status={liveRefresh}
          disabled={loading}
          onUpdated={(status) => applyLiveRefresh(status)}
        />

        <details className="scan-advanced">
          <summary>Advanced · FX / fees / provenance</summary>
          <div className="scan-action">
            <button
              className="scan-button"
              type="button"
              disabled={loading}
              aria-busy={loadingMode === "diagnostic"}
              onClick={() => void collect("diagnostic")}
            >
              {loadingMode === "diagnostic" ? "Running full diagnostic…" : "Run full diagnostic"}
            </button>
          </div>
          <p className="scan-advanced-copy">
            Full diagnostic sweep performs broad venue discovery and can take substantially longer
            than the HOT refresh. It is read-only and remains bounded by the diagnostic scan
            envelope.
          </p>
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
          <p className="scan-advanced-copy">
            FX and venue fees come from the paper economics service
            {economics?.data_kind ? ` (${economics.data_kind.replaceAll("_", " ")})` : ""}.
            Missing or stale required inputs fail closed. Standing capital is Matchbook GBP /
            Polymarket USD / Kalshi USD. Optional extra capital limit is a scan-only cap, not live funds.
            Polymarket fees are per-market CLOB metadata, not a global zero. PAPER MODE · no execution.
          </p>
          <div className="matchbook-fee-editor">
            <label className="scan-field">
              <span>Matchbook commission %</span>
              <input
                inputMode="decimal"
                value={matchbookCommissionPercent}
                onChange={(event) => setMatchbookCommissionPercent(event.target.value)}
                aria-label="Matchbook net-profit commission percent"
              />
            </label>
            <div className="scan-action matchbook-fee-actions">
              <button
                className="scan-button"
                type="button"
                disabled={feeSaving}
                onClick={() => void saveMatchbookCommission()}
              >
                {feeSaving ? "Saving…" : "Save"}
              </button>
              <button
                className="pool-reset"
                type="button"
                disabled={feeSaving}
                onClick={() => void resetMatchbookCommission()}
              >
                Reset to provider default
              </button>
            </div>
          </div>
          <p className="scan-advanced-copy">
            Provider default is the UK Matchbook standard 2.00% net-profit commission. A saved
            override is an operator/account assumption, persists across refresh and backend restart,
            and is snapshotted on each paper decision. Reset restores the provider default.
            {economics?.matchbook_fee?.account_assumption
              ? " Currently using an operator/account assumption."
              : " Currently using the provider default."}
            {feeMessage ? ` ${feeMessage}` : ""}
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
            {state.report.issues.length > 0
              ? ` · ${state.report.issues.length} genuine issue${state.report.issues.length === 1 ? "" : "s"}`
              : ""}
          </div>
        ) : null}

        {state.kind === "success" && state.report.config_warnings?.length ? (
          <div className={CONFIG_WARNING_BANNER_CLASS} role="status">
            {state.report.config_warnings.join(" ")}
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
