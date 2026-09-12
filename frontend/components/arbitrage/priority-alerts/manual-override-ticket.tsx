"use client";

import { useMemo, useState } from "react";

import { money, percent, venueLabel } from "../../../lib/priority-alerts/format";
import { scaleTicket } from "../../../lib/priority-alerts/ticket";
import type { AlertOperatorStatus, PriorityAlert } from "../../../lib/priority-alerts/types";
import { useAlertOperatorState } from "./use-alert-operator-state";

export function ManualOverrideTicket({ alert }: { alert: PriorityAlert }) {
  const [amount, setAmount] = useState(String(alert.recommendedSizeGbp));
  const { status, setStatus } = useAlertOperatorState(alert.alertId);
  const parsed = Number(amount);
  const quote = useMemo(() => scaleTicket(alert, parsed), [alert, parsed]);

  function reduceSize() {
    const next = Math.min(Number.isFinite(parsed) ? parsed : alert.recommendedSizeGbp, alert.recommendedSizeGbp);
    const reduced = Math.max(25, roundTo(next * 0.75, 1));
    setAmount(String(Math.min(reduced, alert.maxValidatedSizeGbp)));
    if (status === "PREPARED") setStatus("OPEN");
  }

  function prepareTicket() {
    if (quote.exceedsValidatedSize || parsed <= 0) return;
    setStatus("PREPARED");
  }

  return (
    <section className="pa-ticket">
      <div className="panel-header">
        <div>
          <div className="panel-title">Manual override ticket</div>
          <div className="panel-meta">capital_source = MANUAL_OVERRIDE · Phase 1 stops before any venue order</div>
        </div>
        <span className={`pa-chip ${status === "PREPARED" ? "pa-chip-paper" : "pa-chip-demo"}`}>
          {statusLabel(status)}
        </span>
      </div>

      <div className="pa-ticket-body">
        <label className="pa-amount">
          <span>Proposed manual amount (limiting-leg GBP)</span>
          <input
            inputMode="decimal"
            value={amount}
            onChange={(event) => {
              setAmount(event.target.value);
              if (status === "PREPARED") setStatus("OPEN");
            }}
          />
        </label>
        <div className="pa-amount-meta">
          Recommended {money(alert.recommendedSizeGbp, "GBP")} · validated max {money(alert.maxValidatedSizeGbp, "GBP")} ·
          theoretical {money(alert.maxTheoreticalSizeGbp, "GBP")}
        </div>

        {quote.exceedsValidatedSize ? (
          <div className="pa-fail" role="alert">
            Requested {money(quote.requestedSizeGbp, "GBP")} exceeds the validated executable maximum of{" "}
            {money(alert.maxValidatedSizeGbp, "GBP")}. This size will not fill — reduce it. No ticket can be prepared.
          </div>
        ) : null}

        {!quote.exceedsValidatedSize && parsed > 0 ? (
          <div className="pa-ok">
            Requested size is within the validated executable maximum. Figures below are paper-only.
          </div>
        ) : null}

        <dl className="pa-quote-grid">
          <div>
            <dt>Stake · limiting leg</dt>
            <dd>{Number.isFinite(parsed) ? money(parsed, "GBP") : "—"}</dd>
          </div>
          <div>
            <dt>Total capital</dt>
            <dd>{money(quote.totalCapitalGbp, "GBP")}</dd>
          </div>
          <div>
            <dt>Guaranteed return</dt>
            <dd className="pa-accent">{money(quote.guaranteedReturnGbp, "GBP")}</dd>
          </div>
          <div>
            <dt>Guaranteed profit</dt>
            <dd className="pa-accent">{money(quote.guaranteedProfitGbp, "GBP")}</dd>
          </div>
          <div>
            <dt>ROI</dt>
            <dd>{percent(quote.roi)}</dd>
          </div>
          <div>
            <dt>Within validated max</dt>
            <dd className={quote.withinValidatedMaximum ? "pa-accent" : "pa-danger"}>
              {quote.withinValidatedMaximum ? "Yes" : "No"}
            </dd>
          </div>
        </dl>

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Leg</th>
                <th>Venue</th>
                <th>Odds</th>
                <th>Stake native</th>
                <th>Stake GBP</th>
                <th>Auto pool</th>
                <th>Exceeds auto pool</th>
              </tr>
            </thead>
            <tbody>
              {quote.legs.map((leg) => (
                <tr key={leg.legId} className={leg.isLimiting ? "pa-row-limit" : undefined}>
                  <td className="row-title">
                    {leg.selectionLabel}
                    {leg.isLimiting ? " · limiting" : ""}
                  </td>
                  <td>{venueLabel(leg.venue)}</td>
                  <td>{leg.decimalOdds.toFixed(3)}</td>
                  <td>{money(leg.stakeNative, leg.currency)}</td>
                  <td>{money(leg.stakeGbp, "GBP")}</td>
                  <td>{money(leg.autoPoolNative, leg.currency)}</td>
                  <td className={leg.manualOverrideNative > 0 ? "pa-warning" : "pa-accent"}>
                    {leg.manualOverrideNative > 0 ? money(leg.manualOverrideNative, leg.currency) : "Covered"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="pa-pool-grid">
          {quote.capitalByVenueCurrency.map((item) => (
            <article key={`${item.venue}-${item.currency}`} className="pa-pool-card">
              <div className="pa-pool-venue">
                {venueLabel(item.venue)} · {item.currency}
              </div>
              <div className="pa-pool-line">
                Required {money(item.requiredNative, item.currency)}
              </div>
              <div className="pa-pool-line">
                Auto pool {money(item.autoPoolNative, item.currency)}
              </div>
              <div className="pa-pool-line">
                Manual override {money(item.additionalManualNative, item.currency)}
              </div>
              <div className="pa-chip">{item.capitalSource}</div>
            </article>
          ))}
        </div>

        <div className="pa-override-sum">
          Additional manual capital: {money(quote.additionalManualGbp, "GBP")} GBP + {money(quote.additionalManualUsd, "USD")} USD
          beyond standing automated pools. Native GBP and USD pools are never blended.
        </div>

        <div className="pa-actions">
          <button
            className="pa-button pa-button-primary"
            type="button"
            disabled={quote.exceedsValidatedSize || parsed <= 0 || status === "PREPARED"}
            onClick={prepareTicket}
          >
            PREPARE MANUAL TICKET
          </button>
          <button className="pa-button" type="button" onClick={reduceSize}>
            REDUCE SIZE
          </button>
          <button className="pa-button" type="button" onClick={() => setStatus("SNOOZED")}>
            SNOOZE
          </button>
          <button className="pa-button pa-button-quiet" type="button" onClick={() => setStatus("DISMISSED")}>
            DISMISS
          </button>
        </div>
        <p className="pa-safety">
          No PLACE BET action. This ticket is paper-only and does not call Matchbook, Smarkets, or Polymarket.
        </p>
      </div>
    </section>
  );
}

function statusLabel(status: AlertOperatorStatus): string {
  if (status === "PREPARED") return "MANUAL_OVERRIDE PREPARED";
  if (status === "SNOOZED") return "SNOOZED";
  if (status === "DISMISSED") return "DISMISSED";
  if (status === "AWAITING_EXTERNAL_LEG_CONFIRMATION") return "AWAITING EXTERNAL LEG";
  if (status === "EXTERNAL_LEG_CONFIRMED") return "EXTERNAL LEG CONFIRMED (PAPER)";
  return "OPEN";
}

function roundTo(value: number, digits: number): number {
  const factor = 10 ** digits;
  return Math.round(value * factor) / factor;
}
