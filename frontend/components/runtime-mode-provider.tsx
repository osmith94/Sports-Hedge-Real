"use client";

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import {
  armLiveCanary,
  disarmLiveCanary,
  getRuntimeHealth,
  heartbeatLiveCanary,
  type RuntimeHealth,
} from "../lib/api";
import {
  readModelBadge,
  runtimeModeFromHealth,
  type RuntimeModeView,
} from "../lib/runtime-mode";

const RuntimeModeContext = createContext<RuntimeModeView>(runtimeModeFromHealth(null));

export function useRuntimeMode(): RuntimeModeView {
  return useContext(RuntimeModeContext);
}

export function RuntimeModeProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<RuntimeHealth | null>(null);
  const [settled, setSettled] = useState(false);

  useEffect(() => {
    let stopped = false;

    async function load() {
      try {
        const next = await getRuntimeHealth();
        if (!stopped) {
          setHealth(next);
          setSettled(true);
        }
      } catch {
        if (!stopped) {
          setHealth(null);
          setSettled(true);
        }
      }
    }

    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, []);

  const view = useMemo(
    () => runtimeModeFromHealth(settled ? health : null),
    [health, settled],
  );

  return <RuntimeModeContext.Provider value={view}>{children}</RuntimeModeContext.Provider>;
}

export function RuntimeModeClock() {
  const view = useRuntimeMode();
  const [health, setHealth] = useState<RuntimeHealth | null>(null);
  const [checking, setChecking] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stopped = false;
    async function load() {
      try {
        const next = await getRuntimeHealth();
        if (!stopped) setHealth(next);
      } catch {
        if (!stopped) setHealth(null);
      }
    }
    void load();
    const timer = window.setInterval(() => {
      void load();
      if (health?.live_canary?.runtime_armed) {
        void heartbeatLiveCanary().catch(() => undefined);
      }
    }, 15000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [health?.live_canary?.runtime_armed]);

  const armed = health?.live_canary?.runtime_armed === true;
  const real = view.mode === "real";
  const toggleLabel = !real
    ? null
    : checking
      ? "CHECKING"
      : armed
        ? "EXECUTION ON"
        : "EXECUTION OFF";

  return (
    <div className="execution-toggle-cluster">
      <div className="clock">{view.known ? view.clock : "GBP · UNAVAILABLE"}</div>
      {toggleLabel ? (
        <button
          type="button"
          className={armed ? "execution-toggle execution-toggle-on" : "execution-toggle"}
          aria-label={toggleLabel}
          disabled={checking}
          onClick={() => {
            if (armed) {
              setChecking(true);
              void disarmLiveCanary()
                .then((next) => {
                  setHealth((current) => ({ ...(current ?? {}), live_canary: next, mode: "real", execution_enabled: false }));
                  setError(null);
                })
                .catch((exc: unknown) => setError(exc instanceof Error ? exc.message : "disarm failed"))
                .finally(() => setChecking(false));
              return;
            }
            setConfirmOpen(true);
          }}
        >
          {toggleLabel}
        </button>
      ) : null}
      {error ? <div className="execution-toggle-error">{error}</div> : null}
      {confirmOpen ? (
        <div className="desktop-exit-backdrop" role="dialog" aria-label="REAL-MONEY EXECUTION confirmation">
          <div className="desktop-exit-dialog">
            <h2>REAL-MONEY EXECUTION</h2>
            <p>
              Turning ON may wager real money on Matchbook and Polymarket. First canary: one
              opportunity, £3 opening both legs, £5 session aggregate. Toggle OFF stops new
              submits and does not cancel orders already sent.
            </p>
            <p>Type ARM LIVE CANARY to confirm.</p>
            <ExecutionConfirm
              onCancel={() => setConfirmOpen(false)}
              onConfirm={async (phrase) => {
                setChecking(true);
                setConfirmOpen(false);
                try {
                  const next = await armLiveCanary(phrase);
                  setHealth((current) => ({
                    ...(current ?? {}),
                    mode: "real",
                    execution_enabled: false,
                    live_canary: next,
                  }));
                  const reasons = next?.not_ready_reasons ?? [];
                  if (!next?.runtime_armed) {
                    setError(reasons.join(", ") || "NOT READY");
                  } else {
                    setError(null);
                  }
                } catch (exc: unknown) {
                  setError(exc instanceof Error ? exc.message : "arm failed");
                } finally {
                  setChecking(false);
                }
              }}
            />
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ExecutionConfirm({
  onCancel,
  onConfirm,
}: {
  onCancel: () => void;
  onConfirm: (phrase: string) => Promise<void>;
}) {
  const [phrase, setPhrase] = useState("");
  return (
    <>
      <input
        aria-label="Confirmation phrase"
        value={phrase}
        onChange={(event) => setPhrase(event.target.value)}
      />
      <div className="desktop-exit-actions">
        <button type="button" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="desktop-exit-confirm"
          disabled={phrase !== "ARM LIVE CANARY"}
          onClick={() => void onConfirm(phrase)}
        >
          Confirm live canary
        </button>
      </div>
    </>
  );
}

export function RuntimeModeFooter() {
  const view = useRuntimeMode();
  return (
    <>
      <div className="paper-pill">
        <span className="paper-dot" /> {view.footerMode}
      </div>
      <div className="sidebar-note">{view.footerNote}</div>
    </>
  );
}

export function OperationsConsoleIntro({ liveConnected }: { liveConnected: boolean }) {
  const view = useRuntimeMode();
  return (
    <>
      <div>
        <div className="eyebrow">{view.operationsEyebrow}</div>
        <h1>Operations console</h1>
        <p className="page-subtitle">{view.operationsSubtitle}</p>
      </div>
      <div className="heading-actions">
        <div className="demo-label">{readModelBadge(view, liveConnected)}</div>
      </div>
    </>
  );
}

export function SimulationOnlyChip() {
  const view = useRuntimeMode();
  if (!view.simulationSection) return null;
  return <span className="demo-chip">SIMULATION / PAPER TOOL</span>;
}

export { readModelBadge };
