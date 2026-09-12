"use client";

import { useState } from "react";
import type { PriorityAlert } from "../../../lib/priority-alerts/types";
import {
  validateExternalLegConfirmation,
  type ExternalLegConfirmationDraft,
} from "../../../lib/priority-alerts/external-leg-confirmation";
import { useAlertOperatorState } from "./use-alert-operator-state";
import { useExternalLegConfirmation } from "./use-external-leg-confirmation";

function defaultDraft(alert: PriorityAlert): ExternalLegConfirmationDraft {
  const external = alert.legs.find((leg) => leg.executionPath === "MANUAL_EXTERNAL");
  return {
    venue: external?.venue ?? "polymarket",
    product: `${external?.venue ?? "polymarket"} · ${alert.market.label}`,
    selection: external?.selectionLabel ?? "",
    executedPrice: "",
    executedSize: "",
    currency: external?.currency ?? "USD",
    executedAt: "",
    externalReference: "",
    operatorNote: "",
  };
}

export function ExternalLegWorkflow({ alert }: { alert: PriorityAlert }) {
  const { status, setStatus } = useAlertOperatorState(alert.alertId);
  const { record, saveRecord } = useExternalLegConfirmation(alert.alertId);
  const [confirmation, setConfirmation] = useState<ExternalLegConfirmationDraft>(() => defaultDraft(alert));
  const [error, setError] = useState<string | null>(null);

  const externalLegs = alert.legs.filter((leg) => leg.executionPath === "MANUAL_EXTERNAL");
  const confirmed = status === "EXTERNAL_LEG_CONFIRMED" && record !== undefined;
  const awaitingEconomics = status === "EXTERNAL_LEG_CONFIRMED" && record === undefined;

  if (alert.executionPath !== "MANUAL_EXTERNAL" && externalLegs.length === 0) return null;

  return (
    <section className="external-leg">
      <div className="section-label">
        <span>
          MANUAL_EXTERNAL · {confirmed ? "EXTERNAL_LEG_CONFIRMED" : "AWAITING_EXTERNAL_LEG_CONFIRMATION"}
        </span>
        <span className="demo-chip">DEMO / FIXTURE · PAPER MODE</span>
      </div>
      <p className="section-copy">
        The Polymarket leg does not draw from Sports Hedge AUTO_POOL. Automated counterpart legs stay uncommitted until
        an operator confirms the external fill with executed economics. There is no PLACE BET, wallet signing, or
        geobypass path.
      </p>
      <ul className="opp-meta">
        {externalLegs.map((leg) => (
          <li key={leg.legId}>
            {leg.venue} {leg.selectionLabel} · {leg.currency} · capital_source {leg.capitalSource}
          </li>
        ))}
      </ul>
      {confirmed && record ? (
        <div className="external-leg-record">
          <p className="opp-note">Paper confirmation record (localStorage). Status alone is not treated as a fill.</p>
          <dl>
            <div>
              <dt>Venue / product</dt>
              <dd>
                {record.venue} · {record.product}
              </dd>
            </div>
            <div>
              <dt>Selection</dt>
              <dd>{record.selection || "—"}</dd>
            </div>
            <div>
              <dt>Executed price</dt>
              <dd>{record.executedPrice}</dd>
            </div>
            <div>
              <dt>Executed size</dt>
              <dd>{record.executedSize}</dd>
            </div>
            <div>
              <dt>Currency</dt>
              <dd>{record.currency}</dd>
            </div>
            <div>
              <dt>Execution timestamp</dt>
              <dd>{record.executedAt}</dd>
            </div>
            <div>
              <dt>External reference</dt>
              <dd>{record.externalReference}</dd>
            </div>
            <div>
              <dt>Operator note</dt>
              <dd>{record.operatorNote || "—"}</dd>
            </div>
            <div>
              <dt>Recorded at</dt>
              <dd>{record.recordedAt}</dd>
            </div>
          </dl>
          <p className="opp-note">
            Hedge revalidation: NOT PERFORMED (demo). Remaining Matchbook/Smarkets legs are not re-fetched. Phase 1
            still cannot place or cancel venue orders. This record is not a completed arbitrage.
          </p>
        </div>
      ) : (
        <>
          <button className="scan-button" type="button" onClick={() => setStatus("AWAITING_EXTERNAL_LEG_CONFIRMATION")}>
            PROCEED WITH EXTERNAL COUNTERPARTY
          </button>
          {awaitingEconomics ? (
            <p className="opp-note">
              A previous confirmation status had no economics. Re-enter executed price, size, timestamp and reference.
            </p>
          ) : null}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              const parsed = validateExternalLegConfirmation(confirmation);
              if (!parsed.ok) {
                setError(parsed.error);
                return;
              }
              saveRecord({
                ...parsed.record,
                alertId: alert.alertId,
                recordedAt: new Date().toISOString(),
              });
              setError(null);
              setStatus("EXTERNAL_LEG_CONFIRMED");
            }}
          >
            <label>
              Venue
              <input
                value={confirmation.venue}
                onChange={(event) => setConfirmation({ ...confirmation, venue: event.target.value })}
              />
            </label>
            <label>
              Product
              <input
                value={confirmation.product}
                onChange={(event) => setConfirmation({ ...confirmation, product: event.target.value })}
              />
            </label>
            <label>
              Selection / outcome
              <input
                value={confirmation.selection}
                onChange={(event) => setConfirmation({ ...confirmation, selection: event.target.value })}
              />
            </label>
            <label>
              Executed price
              <input
                inputMode="decimal"
                value={confirmation.executedPrice}
                onChange={(event) => setConfirmation({ ...confirmation, executedPrice: event.target.value })}
              />
            </label>
            <label>
              Executed size
              <input
                inputMode="decimal"
                value={confirmation.executedSize}
                onChange={(event) => setConfirmation({ ...confirmation, executedSize: event.target.value })}
              />
            </label>
            <label>
              Currency
              <input
                value={confirmation.currency}
                onChange={(event) => setConfirmation({ ...confirmation, currency: event.target.value })}
              />
            </label>
            <label>
              Timestamp
              <input
                placeholder="2026-09-12T10:28:00Z"
                value={confirmation.executedAt}
                onChange={(event) => setConfirmation({ ...confirmation, executedAt: event.target.value })}
              />
            </label>
            <label>
              External reference
              <input
                value={confirmation.externalReference}
                onChange={(event) => setConfirmation({ ...confirmation, externalReference: event.target.value })}
              />
            </label>
            <label>
              Operator note
              <input
                value={confirmation.operatorNote}
                onChange={(event) => setConfirmation({ ...confirmation, operatorNote: event.target.value })}
              />
            </label>
            {error ? <p className="opp-note">{error}</p> : null}
            <div className="scan-action">
              <button className="scan-button" type="submit">
                Record paper confirmation
              </button>
            </div>
          </form>
        </>
      )}
    </section>
  );
}
