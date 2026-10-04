"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  PreparablePaperOpportunity,
  PreparedPaperDeployment,
  preparePaperDeployment,
  recommendPaperDeployment,
  simulatePaperFill,
} from "../lib/api";
import { money, percent } from "../lib/format";
import { useRuntimeMode } from "./runtime-mode-provider";

export function PaperDeploymentPreview({
  opportunities,
  onOpened,
  provenance = "live_paper",
  focusOpportunityId,
}: {
  opportunities: PreparablePaperOpportunity[];
  onOpened?: (tradeId: string) => void;
  provenance?: "live_paper" | "fixture_demo";
  focusOpportunityId?: string | null;
}) {
  const defaults = useMemo(
    () =>
      opportunities.filter(
        (item) => item.bet_actionable === true || (item.bet_actionable !== false && item.settlement_equivalent),
      ),
    [opportunities],
  );
  const focused = defaults.find((item) => item.opportunity_id === focusOpportunityId);
  const initial = focused ?? defaults[0];
  const runtime = useRuntimeMode();
  const ticketTitle = runtime.simulationSection
    ? "Bet Ticket · simulation only · not Real execution"
    : "Bet Ticket · paper only";
  const ticketBadge = runtime.simulationSection
    ? "SIMULATION / PAPER TOOL"
    : "PAPER MODE · NO EXECUTION";
  const [opportunityId, setOpportunityId] = useState(initial?.opportunity_id ?? "");
  const selected = defaults.find((item) => item.opportunity_id === opportunityId) ?? initial;
  const [sizeGbp, setSizeGbp] = useState(
    selected?.recommended_size_gbp != null ? String(selected.recommended_size_gbp) : "",
  );
  const [recommended, setRecommended] = useState(
    selected?.recommended_size_gbp != null ? String(selected.recommended_size_gbp) : "",
  );
  const [preview, setPreview] = useState<PreparedPaperDeployment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmBusy, setConfirmBusy] = useState(false);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [openedTradeId, setOpenedTradeId] = useState<string | null>(null);
  const [idempotentRetry, setIdempotentRetry] = useState(false);

  useEffect(() => {
    if (focused && focused.opportunity_id !== opportunityId) {
      setOpportunityId(focused.opportunity_id);
    }
  }, [focused, opportunityId]);

  useEffect(() => {
    if (!opportunityId) return;
    let cancelled = false;
    async function loadRecommended() {
      let text = selected?.recommended_size_gbp != null ? String(selected.recommended_size_gbp) : "";
      if (!text) {
        try {
          const result = await recommendPaperDeployment({ opportunity_id: opportunityId });
          if (cancelled) return;
          if (result.accepted) {
            text = String(result.recommended_size_gbp);
          } else {
            setRecommended("");
            setError(result.bet_blocked_reason ?? result.limiting_constraint_detail ?? "not recommended");
            return;
          }
        } catch (err) {
          if (!cancelled) {
            setRecommended("");
            setError(err instanceof Error ? err.message : "Recommend failed");
          }
          return;
        }
      }
      if (cancelled || !text) return;
      setRecommended(text);
      setSizeGbp((current) => current || text);
      setBusy(true);
      setError(null);
      try {
        const prepared = await preparePaperDeployment({
          opportunity_id: opportunityId,
          requested_size_gbp: text,
        });
        if (cancelled) return;
        setPreview(prepared);
        if (!prepared.accepted) {
          setError(
            prepared.rejection_reason === "requested_size_exceeds_validated_maximum"
              ? `Requested size exceeds maximum validated ${money(prepared.maximum_validated_size_gbp)}. Size was not silently reduced.`
              : (prepared.rejection_reason ?? "rejected"),
          );
        }
      } catch (err) {
        if (!cancelled) {
          setPreview(null);
          setError(err instanceof Error ? err.message : "Prepare failed");
        }
      } finally {
        if (!cancelled) setBusy(false);
      }
    }
    void loadRecommended();
    return () => {
      cancelled = true;
    };
  }, [opportunityId, selected?.recommended_size_gbp]);

  if (!defaults.length) {
    return (
      <article id="paper-deployment" className="opp-card bet-ticket" tabIndex={-1}>
        <span id="bet-ticket" />
        <div className="opp-event">{ticketTitle}</div>
        <p className="section-copy">
          No settlement-equivalent qualified opportunity is preparable on this fixture. Rejected,
          unevaluated, stale or unsupported rows do not expose BET. This panel does not OPEN a trade
          or lock treasury.
        </p>
      </article>
    );
  }

  async function onPrepare(event?: FormEvent) {
    event?.preventDefault();
    if (!opportunityId || !sizeGbp) return;
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
      if (!result.accepted) {
        setError(
          result.rejection_reason === "requested_size_exceeds_validated_maximum"
            ? `Requested size exceeds maximum validated ${money(result.maximum_validated_size_gbp)}. Size was not silently reduced.`
            : (result.rejection_reason ?? "rejected"),
        );
      }
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
    if (preview.execution_seam?.live_cta_available) {
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

  const confirmLabel =
    preview?.execution_seam?.paper_confirm_cta ?? "Confirm paper OPEN";

  return (
    <article id="paper-deployment" className="opp-card bet-ticket" tabIndex={-1}>
      <span id="bet-ticket" />
      <div className="opp-card-top">
        <div>
          <div className="opp-event">{ticketTitle}</div>
          <div className="muted">
            PAPER MODE · modelled · recommended size from allocator constraints · does not lock
            treasury or place orders
          </div>
        </div>
        <span className="status-badge">{ticketBadge}</span>
      </div>
      <p className="section-copy">
        Default amount is the system-recommended GBP deployment, bounded by treasury, executable
        depth, fees/FX and risk. Editing the amount re-prepares exact native legs on the backend. The
        live CTA <code>Place bets via API</code> stays unavailable while{" "}
        <code>execution_enabled=false</code>.
      </p>
      <form className="inventory-grid" onSubmit={onPrepare}>
        <label>
          <div className="metric-label">Opportunity</div>
          <select
            value={opportunityId}
            onChange={(event) => {
              setOpportunityId(event.target.value);
              setPreview(null);
              setSizeGbp("");
            }}
          >
            {defaults.map((item) => (
              <option key={item.opportunity_id} value={item.opportunity_id}>
                {item.market_label || item.opportunity_id}
                {item.solver_model ? ` · ${item.solver_model}` : ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          <div className="metric-label">GBP deployment</div>
          <input
            value={sizeGbp}
            onChange={(event) => setSizeGbp(event.target.value)}
            onBlur={() => {
              if (sizeGbp) void onPrepare();
            }}
            inputMode="decimal"
            aria-label="Requested paper size in GBP"
          />
        </label>
        <div>
          <button type="submit" disabled={busy || !opportunityId || !sizeGbp}>
            {busy ? "Preparing…" : "Prepare paper legs"}
          </button>
        </div>
      </form>
      <p className="section-copy">
        Recommended {recommended ? money(recommended) : "—"}. Operator-entered {sizeGbp ? money(sizeGbp) : "—"}.
        {selected?.maximum_validated_size_gbp != null
          ? ` Maximum validated ${money(selected.maximum_validated_size_gbp)}.`
          : ""}
      </p>
      {error ? <p className="muted">Rejected: {error}</p> : null}
      {preview ? (
        <DeploymentResult
          preview={preview}
          recommended={recommended}
          confirmBusy={confirmBusy}
          confirmError={confirmError}
          openedTradeId={openedTradeId}
          idempotentRetry={idempotentRetry}
          confirmLabel={openedTradeId ? "Retry confirm (idempotent)" : confirmLabel}
          onConfirm={onConfirm}
        />
      ) : null}
    </article>
  );
}

function DeploymentResult({
  preview,
  recommended,
  confirmBusy,
  confirmError,
  openedTradeId,
  idempotentRetry,
  confirmLabel,
  onConfirm,
}: {
  preview: PreparedPaperDeployment;
  recommended: string;
  confirmBusy: boolean;
  confirmError: string | null;
  openedTradeId: string | null;
  idempotentRetry: boolean;
  confirmLabel: string;
  onConfirm: () => void;
}) {
  if (!preview.accepted) {
    return (
      <p className="section-copy">
        Not accepted: {preview.rejection_reason ?? "rejected"}. Maximum validated{" "}
        {money(preview.maximum_validated_size_gbp)}. Recommended {money(preview.recommended_size_gbp)}.
        No treasury lock. No OPEN.
      </p>
    );
  }
  const quoteAge =
    preview.quote_age_ms == null
      ? "—"
      : preview.quote_age_ms < 1000
        ? `${preview.quote_age_ms}ms`
        : `${(preview.quote_age_ms / 1000).toFixed(1)}s`;
  const survivability = preview.survivability;
  return (
    <div>
      <div className="bet-ticket-meta">
        <div>
          <div className="metric-label">Market / settlement</div>
          <div>
            {preview.market_label ?? "—"}
            {preview.settlement_definition ? ` · ${preview.settlement_definition}` : ""}
          </div>
        </div>
        <div>
          <div className="metric-label">Venue pair</div>
          <div>{(preview.venue_pair ?? []).join(" / ") || "—"}</div>
        </div>
        <div>
          <div className="metric-label">Gross / net edge</div>
          <div>
            {percent(preview.gross_edge)} / {percent(preview.net_edge)}
          </div>
        </div>
        <div>
          <div className="metric-label">Guaranteed profit / ROI</div>
          <div>
            {money(preview.guaranteed_profit_gbp)} · {percent(preview.guaranteed_roi)}
          </div>
        </div>
        <div>
          <div className="metric-label">Execution risk</div>
          <div>
            {preview.execution_risk_score ?? "—"}
            {preview.execution_risk_band ? ` · ${preview.execution_risk_band}` : ""}
          </div>
        </div>
        <div>
          <div className="metric-label">Survivability</div>
          <div>
            {survivability?.available
              ? `${survivability.survivability_score ?? "—"} · estimate, not a guarantee${
                  survivability.volatility_regime ? ` · ${survivability.volatility_regime}` : ""
                }`
              : "unavailable on this decision model"}
          </div>
        </div>
        <div>
          <div className="metric-label">Quote age</div>
          <div>
            {quoteAge}
            {preview.quote_age_basis ? ` · ${preview.quote_age_basis}` : ""} · at last evaluation
          </div>
        </div>
        <div>
          <div className="metric-label">Recommended vs entered</div>
          <div>
            {money(preview.recommended_size_gbp ?? recommended)} vs {money(preview.operator_entered_size_gbp ?? preview.requested_size_gbp)}
          </div>
        </div>
      </div>
      <p className="section-copy">
        Applied {money(preview.applied_size_gbp)} of requested {money(preview.requested_size_gbp)}
        . Native requirements {preview.native_requirements_reconciled ? "reconcile" : "do not reconcile"}
        . Opens trade: no. Locks treasury: no.
      </p>
      <div className="table-wrap">
      <table className="ops-table">
        <thead>
          <tr>
            <th>Venue / side</th>
            <th>Outcome</th>
            <th>Odds</th>
            <th>Native stake</th>
            <th>GBP capital</th>
            <th>Depth consumed</th>
            <th>Fee / FX</th>
            <th>Source / fill</th>
          </tr>
        </thead>
        <tbody>
          {preview.legs.map((leg) => (
            <tr key={`${leg.venue}-${leg.outcome}-${leg.source_market_id}`}>
              <td>
                {leg.venue}
                {leg.action ? ` · ${leg.action}` : ""}
                <div className="muted">{leg.native_currency}</div>
              </td>
              <td>{leg.outcome}</td>
              <td>{leg.displayed_odds ?? "—"}</td>
              <td>{money(leg.stake_native, leg.native_currency === "USD" ? "USD" : "GBP")}</td>
              <td>{money(leg.capital_reporting)}</td>
              <td>
                {leg.displayed_depth_native == null
                  ? "—"
                  : `${money(leg.displayed_depth_native, leg.native_currency === "USD" ? "USD" : "GBP")} · ${
                      leg.depth_consumed_pct == null ? "—" : `${Number(leg.depth_consumed_pct).toFixed(1)}%`
                    }`}
              </td>
              <td>
                {leg.venue_fee == null
                  ? leg.cost_status
                  : `${money(leg.venue_fee, leg.native_currency === "USD" ? "USD" : "GBP")} · ${leg.fee_basis ?? ""}`}
                <div className="muted">
                  FX {leg.fx_gbp_per_unit ?? "—"}
                  {leg.fx_source ? ` · ${leg.fx_source}` : ""}
                </div>
              </td>
              <td>
                {leg.capital_source.replaceAll("_", " ")}
                <div className="muted">{leg.execution_mode.replaceAll("_", " ")}</div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
      {preview.fx_assumptions?.length ? (
        <p className="section-copy">
          FX assumptions:{" "}
          {preview.fx_assumptions
            .map(
              (row) =>
                `${row.currency} ${row.gbp_per_unit} · ${row.source}${
                  row.source_date ? ` ${row.source_date}` : ""
                }`,
            )
            .join(" · ")}
          . Missing FX would fail closed.
        </p>
      ) : null}
      {preview.treasury_remaining?.length ? (
        <p className="section-copy">
          Treasury remaining after proposed lock (modelled, not yet locked):{" "}
          {preview.treasury_remaining
            .map(
              (row) =>
                `${row.venue} ${money(row.free_balance, row.currency === "USD" ? "USD" : "GBP")} free`,
            )
            .join(" · ")}
          .
        </p>
      ) : null}
      <p className="section-copy">
        Confirm revalidates this accepted size against current economics, depth, risk and treasury.
        If the valid amount changed, prepare again. Confirmation does not place venue orders.
      </p>
      <div className="bet-ticket-actions">
        <button
          type="button"
          disabled={confirmBusy || !preview.prepared_deployment_id || preview.places_orders}
          onClick={onConfirm}
        >
          {confirmBusy ? "Confirming…" : confirmLabel}
        </button>
        <button type="button" disabled title={preview.execution_seam?.live_blocked_reason ?? "phase 1 paper only"}>
          {preview.execution_seam?.live_cta ?? "Place bets via API"}
        </button>
      </div>
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

export const BetTicket = PaperDeploymentPreview;
