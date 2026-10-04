export type RuntimeHealth = {
  mode?: string | null;
  execution_enabled?: boolean | null;
};

export type RuntimeModeName = "paper" | "real" | "unknown";

export type RuntimeModeView = {
  known: boolean;
  mode: RuntimeModeName;
  executionEnabled: boolean | null;
  clock: string;
  subtitle: string;
  footerMode: string;
  footerNote: string;
  operationsEyebrow: string;
  operationsSubtitle: string;
  readModelLabel: string;
  offlineReadModelLabel: string;
  simulationSection: boolean;
};

const UNKNOWN: RuntimeModeView = {
  known: false,
  mode: "unknown",
  executionEnabled: null,
  clock: "GBP · RUNTIME STATUS UNKNOWN",
  subtitle: "runtime status unknown",
  footerMode: "RUNTIME STATUS UNKNOWN",
  footerNote: "Runtime status could not be read from /health.",
  operationsEyebrow: "Arbitrage operations",
  operationsSubtitle: "Runtime status unknown. Execution state is unknown until /health responds.",
  readModelLabel: "RUNTIME STATUS UNKNOWN",
  offlineReadModelLabel: "RUNTIME STATUS UNKNOWN",
  simulationSection: false,
};

export function runtimeModeFromHealth(
  health: RuntimeHealth | null | undefined,
): RuntimeModeView {
  const mode = String(health?.mode ?? "").trim().toLowerCase();
  const enabled = health?.execution_enabled;
  if ((mode !== "paper" && mode !== "real") || typeof enabled !== "boolean") {
    return UNKNOWN;
  }
  if (mode === "paper") {
    const execution = enabled ? "EXECUTION ENABLED" : "NO EXECUTION";
    return {
      known: true,
      mode: "paper",
      executionEnabled: enabled,
      clock: `GBP · PAPER MODE · ${execution}`,
      subtitle: "paper operations terminal",
      footerMode: "PAPER MODE",
      footerNote: enabled
        ? "Paper mode reports execution enabled. Confirm /health before treating any control as live."
        : "Research and simulation only. Live execution is disabled at the application boundary.",
      operationsEyebrow: "Arbitrage operations",
      operationsSubtitle:
        "Paper treasury, venue feeds, live pairwise discovery and positions. One operator surface.",
      readModelLabel: "LIVE PAPER READ MODEL",
      offlineReadModelLabel: "PAPER API OFFLINE",
      simulationSection: false,
    };
  }
  const execution = enabled ? "EXECUTION ENABLED" : "EXECUTION DISABLED";
  return {
    known: true,
    mode: "real",
    executionEnabled: enabled,
    clock: `GBP · REAL MODE · ${execution}`,
    subtitle: "real operations terminal",
    footerMode: "REAL MODE",
    footerNote: enabled ? "LIVE ORDER EXECUTION ENABLED" : "LIVE ORDER EXECUTION DISABLED",
    operationsEyebrow: "Operations console",
    operationsSubtitle: enabled
      ? "Live venue feeds, cross-venue discovery, pricing and execution monitoring. Live order execution is enabled."
      : "Live venue feeds, cross-venue discovery, pricing and execution monitoring. Live order execution currently disabled.",
    readModelLabel: "LIVE VENUE READ MODEL",
    offlineReadModelLabel: "VENUE READ MODEL OFFLINE",
    simulationSection: true,
  };
}

export function readModelBadge(view: RuntimeModeView, liveConnected: boolean): string {
  return liveConnected ? view.readModelLabel : view.offlineReadModelLabel;
}
