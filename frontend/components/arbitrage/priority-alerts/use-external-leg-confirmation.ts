"use client";

import { useCallback, useEffect, useState } from "react";

import {
  EXTERNAL_LEG_CONFIRMATION_STORAGE_KEY,
  type ExternalLegConfirmationRecord,
} from "../../../lib/priority-alerts/external-leg-confirmation";

type StoredState = Record<string, ExternalLegConfirmationRecord>;

function readStore(): StoredState {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(EXTERNAL_LEG_CONFIRMATION_STORAGE_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as StoredState;
  } catch {
    return {};
  }
}

function writeStore(state: StoredState): void {
  window.localStorage.setItem(EXTERNAL_LEG_CONFIRMATION_STORAGE_KEY, JSON.stringify(state));
}

export function useExternalLegConfirmation(alertId: string): {
  record: ExternalLegConfirmationRecord | undefined;
  saveRecord: (record: ExternalLegConfirmationRecord) => void;
} {
  const [record, setRecord] = useState<ExternalLegConfirmationRecord | undefined>(undefined);

  useEffect(() => {
    setRecord(readStore()[alertId]);
  }, [alertId]);

  const saveRecord = useCallback(
    (next: ExternalLegConfirmationRecord) => {
      setRecord(next);
      const store = readStore();
      store[alertId] = next;
      writeStore(store);
    },
    [alertId],
  );

  return { record, saveRecord };
}
