"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import {
  EconomicsStatus,
  EconomicsVenueCostRow,
  LiveRefreshStatus,
  PaperCollectionReport,
  PaperCollectionRequest,
  UNIVERSE_LIVE_WORKING_SET_CLEAR_COPY,
  UniverseRunMode,
  clearPaperUniverse,
  getEconomicsStatus,
  getLiveRefreshStatus,
  pauseBackgroundPricing,
  pauseUniverseSchedule,
  resetMatchbookFee,
  resumeBackgroundPricing,
  resumePaperScanner,
  resumeUniverseSchedule,
  runPaperBackgroundRefresh,
  runPaperCollection,
  runPaperHotRefresh,
  runPaperUniverse,
  saveMatchbookFee,
  saveOperatorScannerSettings,
  saveUniverseScope,
  stopPaperScanner,
} from "../lib/api";
import { dualScanStatusLines } from "../lib/scan-status-display";
import { CONFIG_WARNING_BANNER_CLASS } from "../lib/config-warning-display";
import { applyLatestLiveRefresh, createLiveRefreshPollGuard } from "../lib/live-refresh-poll-guard";
import { venueHealthIsDegraded } from "../lib/venue-health-display";
import { ActiveTradeLog } from "./active-trade-log";
import { FootballCompetitionsModal } from "./football-competitions-modal";
import { splitUniverseDraft } from "../lib/competition-modal-draft";
import { LiveScanPulse, LiveScanPulsePhase } from "./live-scan-pulse";
import { VenueLaneControls } from "./venue-lane-controls";

type ScanState =
  | { kind: "idle" }
  | { kind: "success"; report: PaperCollectionReport }
  | { kind: "error"; message: string };

type ScanMode = "hot" | "diagnostic" | "background";

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

function clampHotTargetRefreshSeconds(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_HOT_TARGET_REFRESH_SECONDS;
  return Math.min(60, Math.max(5, Math.round(value)));
}

function clampUniverseDiscoveryRefreshSeconds(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_UNIVERSE_DISCOVERY_REFRESH_SECONDS;
  return Math.min(3600, Math.max(60, Math.round(value)));
}

const DEFAULT_MIN_NET_ARB_PERCENT = "1.00";
const DEFAULT_MAX_RISK = "60";
const DEFAULT_HOT_TARGET_REFRESH_SECONDS = 10;
const DEFAULT_UNIVERSE_DISCOVERY_REFRESH_SECONDS = 3600;
const DEFAULT_MAX_ALLOCATED_PER_TRADE_GBP = "1000";

function minNetPercentFromRate(value: string | number | null | undefined): string {
  if (value == null || value === "") return DEFAULT_MIN_NET_ARB_PERCENT;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return DEFAULT_MIN_NET_ARB_PERCENT;
  return (parsed * 100).toFixed(2);
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
  const [minNetArbPercent, setMinNetArbPercent] = useState(DEFAULT_MIN_NET_ARB_PERCENT);
  const [outrightMinNetArbPercent, setOutrightMinNetArbPercent] = useState("");
  const [maxRisk, setMaxRisk] = useState(DEFAULT_MAX_RISK);
  const [maxAllocatedPerTrade, setMaxAllocatedPerTrade] = useState(DEFAULT_MAX_ALLOCATED_PER_TRADE_GBP);
  const [loadingMode, setLoadingMode] = useState<ScanMode | null>(null);
  const [state, setState] = useState<ScanState>({ kind: "idle" });
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [hotTargetDraft, setHotTargetDraft] = useState(String(DEFAULT_HOT_TARGET_REFRESH_SECONDS));
  const [universeRefreshDraft, setUniverseRefreshDraft] = useState(
    String(DEFAULT_UNIVERSE_DISCOVERY_REFRESH_SECONDS),
  );
  const [settingsDirty, setSettingsDirty] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [scannerControlBusy, setScannerControlBusy] = useState(false);
  const [universeScheduleBusy, setUniverseScheduleBusy] = useState(false);
  const [backgroundPauseBusy, setBackgroundPauseBusy] = useState(false);
  const [universeRunMode, setUniverseRunMode] = useState<UniverseRunMode>("update");
  const [universeActionBusy, setUniverseActionBusy] = useState<"run" | "clear" | null>(null);
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null);
  const [competitionsOpen, setCompetitionsOpen] = useState(false);
  const [competitionsSaving, setCompetitionsSaving] = useState(false);
  const [competitionsError, setCompetitionsError] = useState<string | null>(null);
  const firstRunPromptedRef = useRef(false);
  const [lastCompletedAt, setLastCompletedAt] = useState<string | null>(null);
  const [lastDurationMs, setLastDurationMs] = useState<number | null>(null);
  const [liveRefresh, setLiveRefresh] = useState<LiveRefreshStatus | null>(null);
  const [venueHealth, setVenueHealth] = useState<Record<string, string> | null>(null);
  const [completeFlash, setCompleteFlash] = useState(false);
  const [nowMs, setNowMs] = useState<number | null>(null);
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
    (status: LiveRefreshStatus, options?: { forceSettings?: boolean }) => {
      const saved = status.operator_settings;
      const hotTarget =
        saved?.hot_target_refresh_seconds ??
        status.hot?.target_refresh_seconds ??
        DEFAULT_HOT_TARGET_REFRESH_SECONDS;
      if (hotTarget && (!settingsDirty || options?.forceSettings)) {
        setHotTargetDraft(String(clampHotTargetRefreshSeconds(hotTarget)));
      }
      const universeRefresh =
        saved?.universe_discovery_refresh_seconds ??
        saved?.universe_cadence_seconds ??
        status.universe?.discovery_refresh_seconds ??
        status.universe?.cadence_seconds ??
        DEFAULT_UNIVERSE_DISCOVERY_REFRESH_SECONDS;
      if (universeRefresh && (!settingsDirty || options?.forceSettings)) {
        setUniverseRefreshDraft(String(clampUniverseDiscoveryRefreshSeconds(universeRefresh)));
      }
      if (saved && (!settingsDirty || options?.forceSettings)) {
        setMinNetArbPercent(minNetPercentFromRate(saved.min_net_edge));
        if (saved.outright_min_net_edge == null || saved.outright_min_net_edge === "") {
          setOutrightMinNetArbPercent("");
        } else {
          setOutrightMinNetArbPercent(minNetPercentFromRate(saved.outright_min_net_edge));
        }
        setMaxRisk(String(saved.max_execution_risk ?? DEFAULT_MAX_RISK));
        if (saved.max_allocated_per_trade_gbp != null && saved.max_allocated_per_trade_gbp !== "") {
          setMaxAllocatedPerTrade(String(saved.max_allocated_per_trade_gbp));
        } else {
          setMaxAllocatedPerTrade(DEFAULT_MAX_ALLOCATED_PER_TRADE_GBP);
        }
        if (options?.forceSettings) setSettingsDirty(false);
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
    [router, settingsDirty],
  );

  useEffect(() => {
    const scope = liveRefresh?.universe_scope;
    if (!scope?.needs_first_run_confirmation || firstRunPromptedRef.current) return;
    firstRunPromptedRef.current = true;
    setCompetitionsOpen(true);
  }, [liveRefresh?.universe_scope]);

  const collect = useCallback(async (mode: ScanMode) => {
    if (liveRefresh?.scanner_stopped) return;
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
  }, [applyLiveRefresh, buildPayload, liveRefresh?.scanner_stopped, refreshEconomics, router]);

  const pollLiveStatus = useCallback(async () => {
    try {
      await applyLatestLiveRefresh(
        liveRefreshPollGuardRef.current,
        getLiveRefreshStatus,
        applyLiveRefresh,
      );
    } catch {
      // Status endpoint down: keep prior HOT/BACKGROUND/UNIVERSE facts.
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
      // Status endpoint down: keep the scan-interval and reprice defaults.
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
      return undefined;
    }
    void pollLiveStatus();
    const timer = window.setInterval(() => {
      void pollLiveStatus();
    }, 2000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, liveRefresh?.server_loop_enabled, pollLiveStatus]);

  useEffect(() => {
    if (!autoRefresh || loadingMode !== null) return undefined;
    setNowMs(Date.now());
    const timer = window.setInterval(() => setNowMs(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadingMode]);

  async function saveOperatorSettings() {
    setSettingsSaving(true);
    setSettingsMessage(null);
    try {
      const minNet = optionalPercentRate(minNetArbPercent, "Minimum net arb");
      if (!minNet) {
        throw new Error("Minimum net arb is required.");
      }
      const outrightMinNet = optionalPercentRate(
        outrightMinNetArbPercent,
        "Outright minimum net arb",
      );
      const risk = Number(maxRisk);
      if (!Number.isInteger(risk) || risk < 0 || risk > 100) {
        throw new Error("Maximum execution risk must be a whole number from 0 to 100.");
      }
      const hotTarget = clampHotTargetRefreshSeconds(Number(hotTargetDraft));
      const universeRefresh = clampUniverseDiscoveryRefreshSeconds(Number(universeRefreshDraft));
      const allocated = optionalPositive(maxAllocatedPerTrade, "Max allocated per trade");
      if (!allocated) {
        throw new Error("Max allocated per trade is required.");
      }
      const status = await saveOperatorScannerSettings({
        min_net_edge: minNet,
        outright_min_net_edge: outrightMinNet ?? null,
        max_execution_risk: risk,
        hot_target_refresh_seconds: hotTarget,
        universe_discovery_refresh_seconds: universeRefresh,
        max_allocated_per_trade_gbp: allocated,
      });
      applyLiveRefresh(status, { forceSettings: true });
      setSettingsDirty(false);
      setSettingsMessage("Saved scanner settings. Subsequent server-owned work will use them.");
    } catch (error) {
      setSettingsMessage(error instanceof Error ? error.message : "Could not save scanner settings.");
    } finally {
      setSettingsSaving(false);
    }
  }

  async function toggleScannerStopped() {
    setScannerControlBusy(true);
    setSettingsMessage(null);
    try {
      const status = liveRefresh?.scanner_stopped
        ? await resumePaperScanner()
        : await stopPaperScanner();
      applyLiveRefresh(status, { forceSettings: true });
    } catch (error) {
      setSettingsMessage(
        error instanceof Error ? error.message : "Could not change scanner run state.",
      );
    } finally {
      setScannerControlBusy(false);
    }
  }

  async function toggleBackgroundPricingPaused() {
    setBackgroundPauseBusy(true);
    setSettingsMessage(null);
    try {
      const status = liveRefresh?.background_pricing_paused
        ? await resumeBackgroundPricing()
        : await pauseBackgroundPricing();
      applyLiveRefresh(status, { forceSettings: true });
    } catch (error) {
      setSettingsMessage(
        error instanceof Error ? error.message : "Could not change BACKGROUND pricing pause.",
      );
    } finally {
      setBackgroundPauseBusy(false);
    }
  }

  async function toggleUniverseSchedulePaused() {
    setUniverseScheduleBusy(true);
    setSettingsMessage(null);
    try {
      const status = liveRefresh?.universe_scans_paused
        ? await resumeUniverseSchedule()
        : await pauseUniverseSchedule();
      applyLiveRefresh(status, { forceSettings: true });
    } catch (error) {
      setSettingsMessage(
        error instanceof Error
          ? error.message
          : "Could not change scheduled UNIVERSE scan pause.",
      );
    } finally {
      setUniverseScheduleBusy(false);
    }
  }

  async function applyCompetitionScope(
    codes: string[],
    runUniverseNow: boolean,
    saveAsDefault: boolean,
  ) {
    setCompetitionsSaving(true);
    setCompetitionsError(null);
    try {
      const catalog = liveRefresh?.universe_scope?.catalog ?? [];
      const split = splitUniverseDraft(codes, catalog);
      const status = await saveUniverseScope({
        selected_competition_codes: split.selected_competition_codes,
        selected_season_scope_codes: split.selected_season_scope_codes,
        sport: "football",
        run_universe_now: runUniverseNow,
        save_as_default: saveAsDefault,
      });
      applyLiveRefresh(status, { forceSettings: true });
      setCompetitionsOpen(false);
      setSettingsMessage(
        runUniverseNow
          ? saveAsDefault
            ? "Saved football competition default and requested UNIVERSE now."
            : "Applied football competition scope for this session and requested UNIVERSE now."
          : saveAsDefault
            ? "Saved football competition startup default. UNIVERSE discovery will use it on the next generation."
            : "Applied football competition scope for this session. Saved startup default is unchanged.",
      );
    } catch (error) {
      setCompetitionsError(
        error instanceof Error ? error.message : "Could not save football competition scope.",
      );
    } finally {
      setCompetitionsSaving(false);
    }
  }

  async function runManualBackground() {
    if (liveRefresh?.scanner_stopped) return;
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    liveRefreshPollGuardRef.current.begin();
    setLoadingMode("background");
    setState({ kind: "idle" });
    try {
      const status = await runPaperBackgroundRefresh();
      applyLiveRefresh(status);
      setCompleteFlash(true);
      setNowMs(Date.now());
    } catch (error) {
      setState({
        kind: "error",
        message: error instanceof Error ? error.message : "BACKGROUND pricing failed.",
      });
    } finally {
      inFlightRef.current = false;
      setLoadingMode(null);
      router.refresh();
    }
  }

  async function runUniverseNow() {
    if (liveRefresh?.scanner_stopped) return;
    if (universeActionBusy) return;
    if (universeRunMode === "clear_update") {
      const confirmed =
        typeof window === "undefined"
          ? true
          : window.confirm(
              `${UNIVERSE_LIVE_WORKING_SET_CLEAR_COPY} Then starts a fresh selected-scope generation.`,
            );
      if (!confirmed) return;
    }
    setUniverseActionBusy("run");
    try {
      const status = await runPaperUniverse({ mode: universeRunMode });
      applyLiveRefresh(status);
      const pending = status.universe_scope?.manual_universe_state === "pending";
      setSettingsMessage(
        universeRunMode === "clear_update"
          ? pending
            ? "Cleared live UNIVERSE working set · queued one fresh selected-scope generation."
            : "Cleared live UNIVERSE working set and requested a fresh selected-scope generation."
          : pending
            ? "UNIVERSE already running · queued one fresh selected-scope generation."
            : "UNIVERSE generation requested for the current selected scope.",
      );
    } catch (error) {
      setSettingsMessage(
        error instanceof Error ? error.message : "Could not request UNIVERSE now.",
      );
    } finally {
      setUniverseActionBusy(null);
    }
  }

  async function clearUniverseWorkingSet() {
    if (universeActionBusy) return;
    const confirmed =
      typeof window === "undefined"
        ? true
        : window.confirm(UNIVERSE_LIVE_WORKING_SET_CLEAR_COPY);
    if (!confirmed) return;
    setUniverseActionBusy("clear");
    try {
      const status = await clearPaperUniverse();
      applyLiveRefresh(status);
      setSettingsMessage("Cleared live UNIVERSE working set. History, catalogue, PAPER trades and Treasury are preserved.");
    } catch (error) {
      setSettingsMessage(
        error instanceof Error ? error.message : "Could not clear UNIVERSE.",
      );
    } finally {
      setUniverseActionBusy(null);
    }
  }

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
  const serverOwned = Boolean(liveRefresh?.server_loop_enabled);
  const scannerStopped = Boolean(liveRefresh?.scanner_stopped);
  const universeScansPaused = Boolean(liveRefresh?.universe_scans_paused);
  const backgroundPricingPaused = Boolean(liveRefresh?.background_pricing_paused);
  const nextHotMs = liveRefresh?.hot?.next_due_at
    ? Date.parse(liveRefresh.hot.next_due_at)
    : Number.NaN;
  const nextRefreshSeconds = !autoRefresh || nowMs == null || scannerStopped
    ? null
    : Number.isFinite(nextHotMs)
      ? Math.max(0, Math.ceil((nextHotMs - nowMs) / 1000))
      : null;
  const pulsePhase: LiveScanPulsePhase = loading
    ? "scanning"
    : state.kind === "error"
      ? "error"
      : scannerStopped
        ? "stopped"
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
            Server-owned HOT pricing, BACKGROUND pricing and UNIVERSE discovery plus manual HOT refresh and bounded diagnostics.
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
              onChange={(event) => {
                setMinNetArbPercent(event.target.value);
                setSettingsDirty(true);
              }}
              placeholder="1.00"
              aria-label="Minimum net arbitrage trigger percent"
            />
          </label>
          <label className="scan-field scan-field-outright">
            <span>Outright Min net arb %</span>
            <input
              inputMode="decimal"
              value={outrightMinNetArbPercent}
              onChange={(event) => {
                setOutrightMinNetArbPercent(event.target.value);
                setSettingsDirty(true);
              }}
              placeholder="not set"
              aria-label="Outright or season minimum net arbitrage trigger percent"
              title="COMPETITION_SEASON / outright markets only. Leave empty until the owner sets a value. Unconfigured fails closed and never inherits fixture Min Net Arb."
            />
          </label>
          <label className="scan-field scan-field-compact">
            <span>Max risk</span>
            <input
              inputMode="numeric"
              value={maxRisk}
              onChange={(event) => {
                setMaxRisk(event.target.value);
                setSettingsDirty(true);
              }}
              aria-label="Maximum execution risk score"
            />
          </label>
          <label className="scan-field scan-field-compact">
            <span>HOT target refresh s</span>
            <input
              inputMode="numeric"
              value={hotTargetDraft}
              onChange={(event) => {
                setHotTargetDraft(event.target.value);
                setSettingsDirty(true);
              }}
              onBlur={() =>
                setHotTargetDraft(String(clampHotTargetRefreshSeconds(Number(hotTargetDraft))))
              }
              aria-label="HOT target refresh interval seconds"
              title="Desired time between HOT passes. A pass that finishes early waits out the remainder. A pass that runs long finishes and starts the next one immediately. Safe range 5–60 seconds. Default 10."
            />
          </label>
          <label className="scan-field scan-field-compact">
            <span>UNIVERSE discovery refresh s</span>
            <input
              inputMode="numeric"
              value={universeRefreshDraft}
              onChange={(event) => {
                setUniverseRefreshDraft(event.target.value);
                setSettingsDirty(true);
              }}
              onBlur={() =>
                setUniverseRefreshDraft(
                  String(clampUniverseDiscoveryRefreshSeconds(Number(universeRefreshDraft))),
                )
              }
              aria-label="UNIVERSE discovery refresh seconds"
              title="Fresh UNIVERSE discovery restart interval after a terminal-complete generation. Safe range 60–3600 seconds. Default 3600. Not radar TTL, the ~8s worker cooldown, or generation budget."
            />
          </label>
          <label className="scan-field scan-field-compact">
            <span>Max £ / trade</span>
            <input
              inputMode="decimal"
              value={maxAllocatedPerTrade}
              onChange={(event) => {
                setMaxAllocatedPerTrade(event.target.value);
                setSettingsDirty(true);
              }}
              placeholder="1000"
              aria-label="Maximum allocated pounds per trade"
              title="Cumulative capital cap for one paper trade including top-ups. Default £1,000."
            />
          </label>
          <label className="scan-refresh">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(event) => setAutoRefresh(event.target.checked)}
              aria-label="Auto refresh scanner status view"
            />
            Auto refresh view
          </label>
          <LiveScanPulse
            phase={pulsePhase}
            nextRefreshSeconds={nextRefreshSeconds}
            venueHealth={venueHealth}
            errorMessage={state.kind === "error" ? state.message : null}
          />
          <div className="scan-action">
            <button
              className="scan-button"
              type="submit"
              disabled={loading || scannerStopped}
              aria-busy={loadingMode === "hot"}
              title={scannerStopped ? "Scanner stopped by operator" : undefined}
            >
              {loadingMode === "hot" ? "Scanning… Refreshing HOT…" : "Manual HOT refresh"}
            </button>
          </div>
        </div>
        <div className="scan-lane-actions">
          <div className="scan-competitions-summary">
            <button
              className="scan-button-secondary"
              type="button"
              onClick={() => {
                setCompetitionsError(null);
                setCompetitionsOpen(true);
              }}
              aria-label="Football competitions"
            >
              UNIVERSE scope · {liveRefresh?.universe_scope?.selected_count ?? 8} selected
            </button>
            {liveRefresh?.universe_scope?.new_competitions_available ? (
              <span className="scan-competitions-badge">New competitions available</span>
            ) : null}
          </div>
          <button
            className="scan-button-secondary"
            type="button"
            disabled={loading || scannerStopped}
            aria-busy={loadingMode === "background"}
            title={scannerStopped ? "Scanner stopped by operator" : undefined}
            onClick={() => void runManualBackground()}
          >
            {loadingMode === "background" ? "Refreshing BACKGROUND…" : "Manual BACKGROUND refresh"}
          </button>
          <label className="scan-field scan-field-compact scan-universe-run-mode">
            <span>UNIVERSE run mode</span>
            <select
              aria-label="UNIVERSE run mode"
              value={universeRunMode}
              disabled={loading || scannerStopped || universeActionBusy !== null}
              onChange={(event) =>
                setUniverseRunMode(event.target.value === "clear_update" ? "clear_update" : "update")
              }
            >
              <option value="update">Update existing</option>
              <option value="clear_update">Clear & update</option>
            </select>
          </label>
          <button
            className="scan-button-secondary"
            type="button"
            disabled={loading || scannerStopped || universeActionBusy !== null}
            aria-busy={universeActionBusy === "run"}
            title={
              scannerStopped
                ? "Scanner stopped by operator"
                : universeRunMode === "clear_update"
                  ? UNIVERSE_LIVE_WORKING_SET_CLEAR_COPY
                  : "Update the current UNIVERSE working set. Does not clear live state."
            }
            onClick={() => void runUniverseNow()}
          >
            {universeActionBusy === "run"
              ? universeRunMode === "clear_update"
                ? "Clearing & updating UNIVERSE…"
                : "Requesting UNIVERSE…"
              : liveRefresh?.universe_scope?.manual_universe_state === "pending"
                ? "UNIVERSE queued"
                : liveRefresh?.universe_scope?.manual_universe_state === "running"
                  ? "UNIVERSE running"
                  : universeRunMode === "clear_update"
                    ? "Clear & update"
                    : "Run UNIVERSE now"}
          </button>
          <button
            className="scan-button-danger"
            type="button"
            disabled={loading || universeActionBusy !== null}
            aria-busy={universeActionBusy === "clear"}
            aria-label="Clear universe"
            title={UNIVERSE_LIVE_WORKING_SET_CLEAR_COPY}
            onClick={() => void clearUniverseWorkingSet()}
          >
            {universeActionBusy === "clear" ? "Clearing UNIVERSE…" : "Clear universe"}
          </button>
          <button
            className={universeScansPaused ? "scan-button" : "scan-button-secondary"}
            type="button"
            disabled={universeScheduleBusy || loading || scannerStopped}
            onClick={() => void toggleUniverseSchedulePaused()}
            aria-label={
              universeScansPaused
                ? "Resume scheduled UNIVERSE scans"
                : "Pause scheduled UNIVERSE scans"
            }
            title={
              scannerStopped
                ? "Scanner stopped by operator"
                : universeScansPaused
                  ? "Resume the persisted UNIVERSE discovery refresh. Does not catch up missed intervals."
                  : "Pause periodic UNIVERSE rediscovery. BACKGROUND, HOT and ACTIVE TRADE continue. Run UNIVERSE now still works."
            }
          >
            {universeScheduleBusy
              ? universeScansPaused
                ? "Resuming UNIVERSE…"
                : "Pausing UNIVERSE…"
              : universeScansPaused
                ? "Resume scheduled UNIVERSE"
                : "Pause scheduled UNIVERSE"}
          </button>
          <button
            className={backgroundPricingPaused ? "scan-button" : "scan-button-secondary"}
            type="button"
            disabled={backgroundPauseBusy || loading || scannerStopped}
            onClick={() => void toggleBackgroundPricingPaused()}
            aria-label={
              backgroundPricingPaused ? "Resume BACKGROUND" : "Pause BACKGROUND"
            }
            title={
              scannerStopped
                ? "Scanner stopped by operator"
                : backgroundPricingPaused
                  ? "Resume BACKGROUND from the existing cursor. Does not restart at row 1."
                  : "Stop new BACKGROUND pricing. In-flight calls finish. HOT, ACTIVE and UNIVERSE continue."
            }
          >
            {backgroundPauseBusy
              ? backgroundPricingPaused
                ? "Resuming BACKGROUND…"
                : "Pausing BACKGROUND…"
              : backgroundPricingPaused
                ? "Resume BACKGROUND"
                : "Pause BACKGROUND"}
          </button>
        </div>
        <div className="scan-ops-actions">
          <button
            className="scan-button-secondary"
            type="button"
            disabled={settingsSaving || loading}
            onClick={() => void saveOperatorSettings()}
          >
            {settingsSaving ? "Saving…" : "Update"}
          </button>
          <button
            className={scannerStopped ? "scan-button" : "scan-button-danger"}
            type="button"
            disabled={scannerControlBusy || loading}
            onClick={() => void toggleScannerStopped()}
            aria-label={scannerStopped ? "Resume scanner" : "Stop scanner"}
          >
            {scannerControlBusy
              ? scannerStopped
                ? "Resuming…"
                : "Stopping…"
              : scannerStopped
                ? "Resume scanner"
                : "Stop scanner"}
          </button>
          {scannerStopped ? (
            <span className="status-badge status-badge-stopped" role="status">
              SCANNER STOPPED · ACTIVE TRADE / HOT pricing / BACKGROUND pricing / UNIVERSE discovery paused
            </span>
          ) : universeScansPaused ? (
            <span className="status-badge" role="status">
              UNIVERSE SCHEDULE PAUSED · BACKGROUND / HOT / ACTIVE TRADE continue
            </span>
          ) : backgroundPricingPaused ? (
            <span className="status-badge" role="status">
              BACKGROUND PAUSED · cursor preserved · HOT / ACTIVE / UNIVERSE continue
            </span>
          ) : null}
        </div>
        {settingsMessage ? (
          <div className="scan-note" role="status">
            {settingsMessage}
          </div>
        ) : null}
        <details className="scan-help">
          <summary>How scanning works</summary>
          <div className="scan-note">
            Manual HOT refresh performs a HOT pricing refresh of current known fixtures.
            It does not rediscover the catalogue. Manual BACKGROUND refresh reprices currently due ACTIVE catalogue rows from exact known IDs.
            Neither HOT nor BACKGROUND rediscover the catalogue or advance UNIVERSE generation state.
            Run UNIVERSE now bypasses only the UNIVERSE discovery refresh wait and uses the real selected-scope generation worker.
            Update existing keeps the current live UNIVERSE working set and coalesces if a chunk is already running.
            Clear & update clears the live UNIVERSE working set only, then starts a fresh selected-scope generation with no reused snapshot, cursor or skip IDs.
            Clear universe clears live UNIVERSE current-state, active generation and checkpoint only.
            Clears the live UNIVERSE working set only. History, catalogue, PAPER trades and Treasury are preserved.
            Pause scheduled UNIVERSE stops the periodic timer only; it does not fake a huge discovery refresh, and the stored discovery refresh stays editable for resume.
            Pause BACKGROUND stops new BACKGROUND pricing slices only. In-flight provider calls finish, the coverage cursor stays, and HOT, ACTIVE and UNIVERSE continue. Resume continues from that cursor.
            Update saves Min Net Arb, Outright Min Net Arb, Max Risk, HOT target refresh and UNIVERSE discovery refresh
            and max allocated per trade for subsequent server-owned work and does not trigger a scan.
            Football competitions Apply changes the current session scope, including season
            markets, and does not itself call providers.
            A material competition-scope change while UNIVERSE is paused coalesces one fresh generation, then remains paused.
            Save this selection as my default is required to persist startup scope across restart.
            HOT target refresh is the desired time between HOT passes (default 10s). An early pass waits out the remainder. A late pass finishes and starts again immediately.
            BACKGROUND runs continuously through the catalogue and has no scan interval or reprice-after control.
            UNIVERSE discovery refresh is how often a fresh discovery generation starts after a terminal-complete generation (default 3600s).
            An incomplete generation resumes on the existing cooldown, not the discovery refresh.
            ACTIVE TRADE reprices open paper
            trades every 5s from exact known IDs. Auto refresh view only polls status.
            Run UNIVERSE now is the explicit manual bypass of the discovery refresh wait.
            Clear universe does not call providers and does not cancel HOT, BACKGROUND or ACTIVE TRADE.
          </div>
        </details>
        <details className="scan-help">
          <summary>Diagnostics / lane status</summary>
          <div className="scan-note scan-status-lines" aria-label="ACTIVE TRADE, HOT pricing, BACKGROUND pricing and UNIVERSE discovery status">
            {dualScanStatusLines(liveRefresh, nowMs).map((line) => (
              <div key={line}>{line}</div>
            ))}
            {autoRefresh
              ? serverOwned
                ? scannerStopped
                  ? " · view refresh on · scanner stopped by operator"
                  : " · auto on · view refresh · server owns HOT / BACKGROUND / UNIVERSE"
                : " · auto on · view refresh"
              : " · view refresh off"}
          </div>
        </details>
        <ActiveTradeLog recentItems={liveRefresh?.active_trade_timeline} compact />
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
              disabled={loading || scannerStopped}
              aria-busy={loadingMode === "diagnostic"}
              title={scannerStopped ? "Scanner stopped by operator" : undefined}
              onClick={() => void collect("diagnostic")}
            >
              {loadingMode === "diagnostic" ? "Running full diagnostic…" : "Run full diagnostic"}
            </button>
          </div>
          <p className="scan-advanced-copy">
            Full diagnostic performs bounded broad venue discovery for operator diagnosis.
            It is not UNIVERSE discovery and does not advance UNIVERSE generation state.
            It is read-only and remains bounded by the diagnostic scan envelope.
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
        <FootballCompetitionsModal
          open={competitionsOpen}
          scope={liveRefresh?.universe_scope ?? null}
          scannerStopped={scannerStopped}
          saving={competitionsSaving}
          errorMessage={competitionsError}
          onClose={() => setCompetitionsOpen(false)}
          onApply={applyCompetitionScope}
        />
      </form>
    </section>
  );
}
