"use client";

import { listPriorityAlerts } from "../../../lib/priority-alerts/provider";
import type { AlertOperatorStatus } from "../../../lib/priority-alerts/types";
import { PriorityAlertCard } from "./priority-alert-card";
import { useAlertOperatorMap } from "./use-alert-operator-state";

export function PriorityAlertList() {
  const alerts = listPriorityAlerts();
  const operatorMap = useAlertOperatorMap();

  const open = alerts.filter((alert) => isOpen(operatorMap[alert.alertId]));
  const parked = alerts.filter((alert) => !isOpen(operatorMap[alert.alertId]));

  return (
    <div className="pa-list">
      <div className="pa-list-grid">
        {open.map((alert) => (
          <PriorityAlertCard key={alert.alertId} alert={alert} />
        ))}
      </div>
      {open.length === 0 ? (
        <div className="empty-live">No open priority alerts. Snoozed or dismissed fixtures are listed below.</div>
      ) : null}
      {parked.length > 0 ? (
        <section className="pa-parked">
          <div className="panel-title">Snoozed / dismissed</div>
          <div className="pa-list-grid">
            {parked.map((alert) => (
              <PriorityAlertCard key={alert.alertId} alert={alert} />
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function isOpen(status: AlertOperatorStatus | undefined): boolean {
  return status === undefined || status === "OPEN" || status === "PREPARED";
}
