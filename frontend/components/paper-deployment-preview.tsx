"use client";

import { FormEvent, useMemo, useState } from "react";

import {
  PreparablePaperOpportunity,
  PreparedPaperDeployment,
  preparePaperDeployment,
  simulatePaperFill,
} from "../lib/api";
import { money } from "../lib/format";

export function PaperDeploymentPreview({
  opportunities,
  onOpened,
  provenance = "live_paper",
}: {
  opportunities: PreparablePaperOpportunity[];
  onOpened?: (tradeId: string) => void;
  provenance?: "live_paper" | "fixture_demo";
}) {
  const defaults = useMemo(
    () => opportunities.filter((item) => item.settlement_equivalent),
    [opportunities],
  );
  const [opportunityId, setOpportunityId] = useState(defaults[0]?.opportunity_id ?? "");
  const [sizeGbp, setSizeGbp] = useState("10");
  const [preview, setPreview] = useState<PreparedPaperDeployment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [openedTradeId, setOpenedTradeId] = useState<string | null>(null);
  const [idempotentRetry, setIdempotentRetry] = useState(false);

  if (!defaults.length) {
    return (
      <article id="paper-deployment" className="opp-card" tabIndex={-1}>
        <div className="opp-event">Fixed-size paper preparation</div>
        <p className="section-copy">
          No settlement-equivalent qualified opportunity is prepared on this fixture yet. This
          panel does not OPEN a trade or lock treasury.
        </p>
      </article>
    );
  }

  async function onPrepare(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setConfirmError(null);
    setOpenedTradeId(null);
    setIdempotentRetry(false);
    try {
      const result = await preparePaperDeployment({
        opportunity_id: opportunityId,
        requested_size_gbp: sizeGbp,
      });
      setPreview(result);
    } catch (err) {
      setPreview(null);
      setError(err instanceof Error ? err.message : "Prepare failed");
    } finally {
      setBusy(false);
    }
  }

  async function onConfirm() {
    if (!preview?.accepted || !preview.prepared_deployment_id) {
      return;
    }
    setConfirmBusy(true);
    setConfirmError(null);
    try {
      const result = (await simulatePaperFill({
        opportunity_id: preview.opportunity_id,
        prepared_deployment_id: preview.prepared_deployment_id,
        requested_size_gbp: String(preview.requested_size_gbp),
        simulate_external: true,
        provenance,
        operator_note: "PAPER-ONLY confirm accepted prepared size; revalidates before lock",
      })) as { trade_id?: string | null; rejection_reason?: string | null };
      if (result.trade_id) {
        setIdempotentRetry(Boolean(openedTradeId) && result.trade_id === openedTradeId);
        setOpenedTradeId(result.trade_id);
        onOpened?.(result.trade_id);
      } else {
        setConfirmError(result.rejection_reason ?? "Confirm did not OPEN");
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "Confirm failed";
      setConfirmError(
        message.includes("prepared_deployment_stale")
          ? "Current economics changed. Prepare again before OPEN."
          : message,
      );
    } finally {
      setConfirmBusy(false);
    }
  }

  return (
    <article id="paper-deployment" className="opp-card" tabIndex={-1}>
      <div className="opp-event">Fixed-size paper preparation</div>
      <p className="section-copy">
        Choose a concrete GBP deployment (example £10). Exact native legs are shown before any
        OPEN. PAPER MODE · modelled · does not lock treasury or place orders. Capital source is
        the paper pool classification; fill mode is the modelled execution path. Paper-simulated
        external is not a live external confirmation.
      </p>
      <form className="inventory-grid" onSubmit={onPrepare}>
        <label>
          <div className="metric-label">Opportunity</div>
          <select value={opportunityId} onChange={(event) => setOpportunityId(event.target.value)}>
            {defaults.map((item) => (
              <option key={item.opportunity_id} value={item.opportunity_id}>
                {item.opportunity_id}
                {item.solver_model ? ` · ${item.solver_model}` : ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          <div className="metric-label">Requested size (GBP equivalent)</div>
          <input
            value={sizeGbp}
            onChange={(event) => setSizeGbp(event.target.value)}
            inputMode="decimal"
            aria-label="Requested paper size in GBP"
          />
        </label>
        <div>
          <button type="submit" disabled={busy || !opportunityId}>
            {busy ? "Preparing…" : "Prepare paper legs"}
          </button>
        </div>
      </form>
      {error ? <p className="muted">Rejected: {error}</p> : null}
      {preview ? (
        <DeploymentResult
          preview={preview}
          confirmBusy={confirmBusy}
          confirmError={confirmError}
          openedTradeId={openedTradeId}
          idempotentRetry={idempotentRetry}
          onConfirm={onConfirm}
        />
      ) : null}
    </article>
  );
}

function DeploymentResult({
  preview,
  confirmBusy,
  confirmError,
  openedTradeId,
  idempotentRetry,
  onConfirm,
}: {
  preview: PreparedPaperDeployment;
  confirmBusy: boolean;
  confirmError: string | null;
  openedTradeId: string | null;
  idempotentRetry: boolean;
  onConfirm: () => void;
}) {
  if (!preview.accepted) {
    return (
      <p className="section-copy">
        Not accepted: {preview.rejection_reason ?? "rejected"}. Maximum validated{" "}
        {money(preview.maximum_validated_size_gbp)}. No treasury lock. No OPEN.
      </p>
    );
  }
  return (
    <div>
      <p className="section-copy">
        Applied {money(preview.applied_size_gbp)} of requested {money(preview.requested_size_gbp)}
        . Native requirements {preview.native_requirements_reconciled ? "reconcile" : "do not reconcile"}
        . Opens trade: no. Locks treasury: no.
      </p>
      <table className="ops-table">
        <thead>
          <tr>
            <th>Venue</th>
            <th>Currency</th>
            <th>Outcome</th>
            <th>Odds</th>
            <th>Native stake</th>
            <th>GBP capital</th>
            <th>Modelled fee</th>
            <th>Capital source</th>
            <th>Fill mode</th>
          </tr>
        </thead>
        <tbody>
          {preview.legs.map((leg) => (
            <tr key={`${leg.venue}-${leg.outcome}-${leg.source_market_id}`}>
              <td>{leg.venue}</td>
              <td>{leg.native_currency}</td>
              <td>{leg.outcome}</td>
              <td>{leg.displayed_odds ?? "—"}</td>
              <td>{money(leg.stake_native, leg.native_currency === "USD" ? "USD" : "GBP")}</td>
              <td>{money(leg.capital_reporting)}</td>
              <td>
                {leg.venue_fee == null
                  ? leg.cost_status
                  : `${money(leg.venue_fee, leg.native_currency === "USD" ? "USD" : "GBP")} · ${leg.fee_basis ?? ""}`}
              </td>
              <td>{leg.capital_source.replaceAll("_", " ")}</td>
              <td>{leg.execution_mode.replaceAll("_", " ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="section-copy">
        Confirm revalidates this accepted size against current economics, depth, risk and treasury.
        If the valid amount changed, prepare again. Confirmation does not place venue orders.
      </p>
      <button
        type="button"
        disabled={confirmBusy || !preview.prepared_deployment_id}
        onClick={onConfirm}
      >
        {confirmBusy
          ? "Confirming…"
          : openedTradeId
            ? "Retry confirm (idempotent)"
            : "Confirm paper OPEN"}
      </button>
      {confirmError ? <p className="muted">Rejected: {confirmError}</p> : null}
      {openedTradeId ? (
        <p className="section-copy">
          Paper OPEN recorded for {openedTradeId}. Native legs locked at the confirmed size.
          {idempotentRetry
            ? " Repeat confirm returned the same trade; no additional fill, lock, or journal."
            : ""}
        </p>
      ) : null}
    </div>
  );
}
