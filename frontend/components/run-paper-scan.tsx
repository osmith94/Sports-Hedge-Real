"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

import {
  PaperCollectionReport,
  PaperCollectionRequest,
  runPaperCollection,
} from "../lib/api";
import { DEFAULT_SCANNER_ASSUMPTIONS } from "../lib/arbitrage-ops";
import { percent } from "../lib/format";

type ScanState =
  | { kind: "idle" }
  | { kind: "success"; report: PaperCollectionReport }
  | { kind: "error"; message: string };

function optionalPositive(value: string, label: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${label} must be greater than zero.`);
  }
  return trimmed;
}

function optionalPercentRate(value: string, label: string): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed < 0 || parsed >= 100) {
    throw new Error(`${label} must be between 0% and 100%.`);
  }
  return String(parsed / 100);
}

function reportSummary(report: PaperCollectionReport): string {
  const eligible = report.paper_decisions.filter(
    (decision) => decision.eligible_for_paper_simulation,
  ).length;
  return `${report.matched_event_pairs} event pair${report.matched_event_pairs === 1 ? "" : "s"} · ${report.matched_market_pairs} market pair${report.matched_market_pairs === 1 ? "" : "s"} · ${eligible} paper-eligible · ${report.issues.length} issue${report.issues.length === 1 ? "" : "s"}`;
}

export function RunPaperScan() {
  const router = useRouter();
  const [usdToGbp, setUsdToGbp] = useState("");
  const [matchbookFeePercent, setMatchbookFeePercent] = useState("");
  const [polymarketFeePercent, setPolymarketFeePercent] = useState("");
  const [capitalLimit, setCapitalLimit] = useState("");
  const [minNetArbPercent, setMinNetArbPercent] = useState(
    String(DEFAULT_SCANNER_ASSUMPTIONS.minimumNetArb * 100),
  );
  const [maxRisk, setMaxRisk] = useState(String(DEFAULT_SCANNER_ASSUMPTIONS.maximumExecutionRisk));
  const [loading, setLoading] = useState(false);
  const [state, setState] = useState<ScanState>({ kind: "idle" });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (loading) return;

    setLoading(true);
    setState({ kind: "idle" });
    try {
      const fxRate = optionalPositive(usdToGbp, "USD→GBP rate");
      const matchbookFee = optionalPercentRate(matchbookFeePercent, "Matchbook fee");
      const polymarketFee = optionalPercentRate(polymarketFeePercent, "Polymarket fee");
      const capital = optionalPositive(capitalLimit, "Capital limit");
      const minNet = optionalPercentRate(minNetArbPercent, "Minimum net arb");
      const risk = Number(maxRisk);
      if (!Number.isInteger(risk) || risk < 0 || risk > 100) {
        throw new Error("Maximum execution risk must be a whole number from 0 to 100.");
      }

      const payload: PaperCollectionRequest = {
        maximum_execution_risk: risk,
      };
      if (capital) payload.capital_limit_gbp = capital;
      if (minNet) payload.minimum_net_edge = minNet;
      if (fxRate) {
        payload.fx_snapshots = [
          { currency: "USD", gbp_per_unit: fxRate, source: "dashboard_input" },
        ];
      }

      const fees: NonNullable<PaperCollectionRequest["fee_snapshots"]> = [];
      if (matchbookFee !== undefined) {
        fees.push({
          venue: "matchbook",
          profit_haircut_rate: matchbookFee,
          source: "dashboard_input",
        });
      }
      if (polymarketFee !== undefined) {
        fees.push({
          venue: "polymarket",
          profit_haircut_rate: polymarketFee,
          source: "dashboard_input",
        });
      }
      if (fees.length) payload.fee_snapshots = fees;

      const report = await runPaperCollection(payload);
      setState({ kind: "success", report });
      router.refresh();
    } catch (error) {
      setState({
        kind: "error",
        message: error instanceof Error ? error.message : "Read-only scan failed.",
      });
    } finally {
      setLoading(false);
    }
  }

  const triggerDisplay = minNetArbPercent.trim()
    ? `${minNetArbPercent.trim()}%`
    : percent(DEFAULT_SCANNER_ASSUMPTIONS.minimumNetArb);

  return (
    <section className="panel scan-control">
      <div className="panel-header">
        <div>
          <div className="panel-title">Paper scanner controls</div>
          <div className="panel-meta">
            Fetch current Matchbook and Polymarket market data, persist history, and run the paper filters.
          </div>
        </div>
        <span className="status-badge">PAPER MODE · NO EXECUTION</span>
      </div>

      <form className="scan-form" onSubmit={submit}>
        <div className="assumption-strip">
          <span>Trigger {triggerDisplay} net arb</span>
          <span>Capital {capitalLimit.trim() ? `£${capitalLimit.trim()}` : "unset"}</span>
          <span>Max risk {maxRisk}/100</span>
          <span>Fees {matchbookFeePercent || polymarketFeePercent ? "dashboard input" : "fail-closed if missing"}</span>
          <span>FX {usdToGbp.trim() ? "dashboard USD→GBP" : "fail-closed if missing"}</span>
        </div>

        <div className="scan-control-grid scan-control-grid-ops">
          <label className="scan-field">
            <span>Min net arb %</span>
            <input
              inputMode="decimal"
              value={minNetArbPercent}
              onChange={(event) => setMinNetArbPercent(event.target.value)}
              placeholder="1.00"
              aria-label="Minimum net arbitrage trigger percent"
            />
          </label>
          <label className="scan-field">
            <span>USD → GBP</span>
            <input
              inputMode="decimal"
              value={usdToGbp}
              onChange={(event) => setUsdToGbp(event.target.value)}
              placeholder="e.g. 0.75"
              aria-label="USD to GBP paper FX rate"
            />
          </label>
          <label className="scan-field">
            <span>Matchbook fee %</span>
            <input
              inputMode="decimal"
              value={matchbookFeePercent}
              onChange={(event) => setMatchbookFeePercent(event.target.value)}
              placeholder="enter assumption"
              aria-label="Matchbook paper fee assumption percent"
            />
          </label>
          <label className="scan-field">
            <span>Polymarket fee %</span>
            <input
              inputMode="decimal"
              value={polymarketFeePercent}
              onChange={(event) => setPolymarketFeePercent(event.target.value)}
              placeholder="enter assumption"
              aria-label="Polymarket paper fee assumption percent"
            />
          </label>
          <label className="scan-field">
            <span>Capital limit £</span>
            <input
              inputMode="decimal"
              value={capitalLimit}
              onChange={(event) => setCapitalLimit(event.target.value)}
              placeholder="optional"
              aria-label="Paper capital limit pounds"
            />
          </label>
          <label className="scan-field">
            <span>Max risk / 100</span>
            <input
              inputMode="numeric"
              value={maxRisk}
              onChange={(event) => setMaxRisk(event.target.value)}
              aria-label="Maximum execution risk score"
            />
          </label>
          <div className="scan-action">
            <button className="scan-button" type="submit" disabled={loading}>
              {loading ? "Scanning…" : "Run read-only scan"}
            </button>
          </div>
        </div>

        <div className="scan-note">
          No orders are submitted. Blank fee or FX assumptions keep affected results diagnostic and ineligible for paper simulation rather than inventing costs.
        </div>

        {state.kind === "success" ? (
          <div className="scan-message scan-message-success" role="status">
            {reportSummary(state.report)}
            {state.report.issues.length > 0 ? (
              <span> · First issue: {state.report.issues[0].detail}</span>
            ) : null}
          </div>
        ) : null}

        {state.kind === "error" ? (
          <div className="scan-message scan-message-error" role="alert">
            {state.message}
          </div>
        ) : null}
      </form>
    </section>
  );
}
