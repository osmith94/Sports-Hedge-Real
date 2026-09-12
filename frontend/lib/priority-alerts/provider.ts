import { PRIORITY_ALERT_FIXTURES } from "./fixtures";
import type { PriorityAlert } from "./types";

export type PriorityAlertProviderMeta = {
  source: "DEMO_FIXTURE";
  paperMode: true;
  label: "DEMO/FIXTURE DATA";
  note: "Typed demo provider until Agent AB backend contracts land. No venue calls.";
};

export function getPriorityAlertProviderMeta(): PriorityAlertProviderMeta {
  return {
    source: "DEMO_FIXTURE",
    paperMode: true,
    label: "DEMO/FIXTURE DATA",
    note: "Typed demo provider until Agent AB backend contracts land. No venue calls.",
  };
}

export function listPriorityAlerts(): PriorityAlert[] {
  return PRIORITY_ALERT_FIXTURES;
}

export function getPriorityAlert(alertId: string): PriorityAlert | null {
  return PRIORITY_ALERT_FIXTURES.find((alert) => alert.alertId === alertId) ?? null;
}

export function getOpenPriorityAlertCount(): number {
  return PRIORITY_ALERT_FIXTURES.length;
}
