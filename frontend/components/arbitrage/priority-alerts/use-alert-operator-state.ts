"use client";

import { useCallback, useEffect, useState } from "react";

import type { AlertOperatorStatus } from "../../../lib/priority-alerts/types";

const STORAGE_KEY = "sports-hedge.priority-alert.operator-state.v1";

type StoredState = Record<string, AlertOperatorStatus>;

function readStore(): StoredState {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as StoredState;
  } catch {
    return {};
  }
}

function writeStore(state: StoredState): void {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
}

export function useAlertOperatorState(alertId: string): {
  status: AlertOperatorStatus;
  setStatus: (status: AlertOperatorStatus) => void;
} {
  const [status, setStatusState] = useState<AlertOperatorStatus>("OPEN");

  useEffect(() => {
    setStatusState(readStore()[alertId] ?? "OPEN");
  }, [alertId]);

  const setStatus = useCallback(
    (next: AlertOperatorStatus) => {
      setStatusState(next);
      const store = readStore();
      store[alertId] = next;
      writeStore(store);
    },
    [alertId],
  );

  return { status, setStatus };
}

export function useAlertOperatorMap(): Record<string, AlertOperatorStatus> {
  const [map, setMap] = useState<StoredState>({});

  useEffect(() => {
    setMap(readStore());
    const onStorage = () => setMap(readStore());
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  return map;
}
