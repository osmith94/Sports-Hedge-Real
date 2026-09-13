"use client";

import { FormEvent, useMemo, useState } from "react";

import {
  PreparablePaperOpportunity,
  PreparedPaperDeployment,
  preparePaperDeployment,
} from "../lib/api";
import { money } from "../lib/format";

export function PaperDeploymentPreview({
  opportunities,
}: {
  opportunities: PreparablePaperOpportunity[];
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

  if (!defaults.length) {
    return (
      <article className="opp-card">
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

  return (
    <article className="opp-card">
      <div className="opp-event">Fixed-size paper preparation</div>
      <p className="section-copy">
        Choose a concrete GBP deployment (example £10). Exact native legs are shown before any
        OPEN. PAPER MODE · modelled · does not lock treasury or place orders.
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
      {preview ? <DeploymentResult preview={preview} /> : null}
    </article>
  );
}

function DeploymentResult({ preview }: { preview: PreparedPaperDeployment }) {
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
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
