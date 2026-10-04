"use client";

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { getRuntimeHealth, type RuntimeHealth } from "../lib/api";
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
  return <div className="clock">{view.clock}</div>;
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
