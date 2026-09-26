"use client";

import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import {
  LiveRefreshStatus,
  PaperScanCycleRecord,
  UniverseCatalogueSnapshot,
  getLiveRefreshStatus,
  getPaperScanCycles,
  getUniverseFixtureCatalogue,
} from "../lib/api";
import { createLiveStatusPoll } from "../lib/live-status-poll";

const LIVE_STATUS_INTERVAL_MS = 2000;
export const SCAN_CYCLE_HISTORY_LIMIT = 50;

export type LiveStatusValue = {
  status: LiveRefreshStatus | null;
  settled: boolean;
  available: boolean;
  catalogue: UniverseCatalogueSnapshot | null;
  catalogueAvailable: boolean;
  scanCycles: PaperScanCycleRecord[];
  scanCyclesAvailable: boolean;
  cycleStamp: string;
  refreshNow: () => Promise<LiveRefreshStatus | null>;
};

const LiveStatusContext = createContext<LiveStatusValue | null>(null);

export function useLiveStatusOptional(): LiveStatusValue | null {
  return useContext(LiveStatusContext);
}

export function useLiveStatus(): LiveStatusValue {
  const value = useContext(LiveStatusContext);
  if (!value) {
    throw new Error("Live status is only available inside LiveStatusProvider");
  }
  return value;
}

function cycleStampOf(status: LiveRefreshStatus | null): string {
  return [
    status?.hot?.last_completed_at ?? "",
    status?.background?.last_completed_at ?? "",
    status?.universe?.last_completed_at ?? "",
  ].join("|");
}

export function LiveStatusProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<LiveRefreshStatus | null>(null);
  const [settled, setSettled] = useState(false);
  const [available, setAvailable] = useState(true);
  const [catalogue, setCatalogue] = useState<UniverseCatalogueSnapshot | null>(null);
  const [catalogueAvailable, setCatalogueAvailable] = useState(true);
  const [scanCycles, setScanCycles] = useState<PaperScanCycleRecord[]>([]);
  const [scanCyclesAvailable, setScanCyclesAvailable] = useState(true);
  const catalogueVersion = useRef<string | null>(null);
  const loadedStamp = useRef<string | null>(null);
  const refreshRef = useRef<() => Promise<LiveRefreshStatus | null>>(async () => null);

  useEffect(() => {
    const poll = createLiveStatusPoll({
      intervalMs: LIVE_STATUS_INTERVAL_MS,
      fetchStatus: getLiveRefreshStatus,
      onStatus: (next) => {
        setStatus(next);
        setSettled(true);
        setAvailable(true);
      },
      onError: () => {
        setSettled(true);
        setAvailable(false);
      },
    });
    refreshRef.current = poll.poll;
    return () => poll.stop();
  }, []);

  const version = status?.universe_catalogue_version ?? "";
  useEffect(() => {
    if (!version || version === catalogueVersion.current) return undefined;
    let cancelled = false;
    getUniverseFixtureCatalogue()
      .then((next) => {
        if (cancelled) return;
        catalogueVersion.current = next.universe_catalogue_version || version;
        setCatalogue(next);
        setCatalogueAvailable(true);
      })
      .catch(() => {
        if (!cancelled) setCatalogueAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, [version]);

  const stamp = cycleStampOf(status);
  useEffect(() => {
    if (loadedStamp.current === stamp && loadedStamp.current !== null) return undefined;
    let cancelled = false;
    getPaperScanCycles(`limit=${SCAN_CYCLE_HISTORY_LIMIT}`)
      .then((cycles) => {
        if (cancelled) return;
        loadedStamp.current = stamp;
        setScanCycles(cycles.slice(0, SCAN_CYCLE_HISTORY_LIMIT));
        setScanCyclesAvailable(true);
      })
      .catch(() => {
        if (!cancelled) setScanCyclesAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, [stamp]);

  const value = useMemo<LiveStatusValue>(
    () => ({
      status,
      settled,
      available: status != null || (settled && available),
      catalogue,
      catalogueAvailable,
      scanCycles,
      scanCyclesAvailable,
      cycleStamp: stamp,
      refreshNow: () => refreshRef.current(),
    }),
    [status, settled, available, catalogue, catalogueAvailable, scanCycles, scanCyclesAvailable, stamp],
  );

  return <LiveStatusContext.Provider value={value}>{children}</LiveStatusContext.Provider>;
}
