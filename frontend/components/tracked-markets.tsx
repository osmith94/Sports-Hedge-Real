"use client";

import { useMemo, useState } from "react";

import { ArbitrageOpportunity } from "../lib/arbitrage-ops";
import {
  distanceToSelectedThresholdPp,
  grossPricesEffectivelyEqual,
  operatorThresholdOptions,
} from "../lib/comfort-threshold";
import { percent, percentPoints } from "../lib/format";

function formatThreshold(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

export function TrackedMarketsBoard({
  items,
  available,
}: {
  items: ArbitrageOpportunity[];
  available: boolean;
}) {
  const backendTriggers = useMemo(() => items.map((item) => item.trigger), [items]);
  const options = useMemo(() => operatorThresholdOptions(backendTriggers), [backendTriggers]);
  const defaultTrigger = items[0]?.trigger ?? 0.01;
  const [selected, setSelected] = useState(defaultTrigger);
  const selectedThreshold = options.includes(selected) ? selected : options[0] ?? defaultTrigger;

  if (!available) {
    return (
      <div className="empty-live-compact">
        Tracked watchlist unavailable. No fabricated fixtures.
      </div>
    );
  }

  if (items.length === 0) {
    return <div className="empty-live-compact">No tracked markets.</div>;
  }

  return (
    <>
      <div className="comfort-bar">
        <div>
          <div className="comfort-kicker">Operator comparison / comfort threshold</div>
          <p className="section-copy">Compare backend net edge to an alternate threshold. Does not resize or reclassify.</p>
        </div>
        <div className="comfort-pills" role="group" aria-label="Operator comfort threshold">
          {options.map((option) => {
            const isBackend = items.some((item) => Math.abs(item.trigger - option) < 1e-9);
            const sameAsBackend = items.every((item) => Math.abs(item.trigger - option) < 1e-9);
            return (
              <button
                className={`comfort-pill ${Math.abs(selectedThreshold - option) < 1e-9 ? "active" : ""}`}
                key={option}
                onClick={() => setSelected(option)}
                type="button"
              >
                {formatThreshold(option)}
                {sameAsBackend ? " · backend trigger" : isBackend ? " · some backend triggers" : " · comparison only"}
              </button>
            );
          })}
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Fixture / market</th>
              <th>Status</th>
              <th>Gross edge</th>
              <th>Current net margin</th>
              <th>Backend trigger</th>
              <th>Distance to backend trigger</th>
              <th>Distance to selected threshold</th>
              <th>Source / last updated</th>
              <th>Strike narrative</th>
              <th>Economics note</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const comparison = distanceToSelectedThresholdPp(item.netArb, selectedThreshold);
              const belowEven = item.netArb !== null && item.netArb < 0;
              const feeVisible =
                belowEven &&
                grossPricesEffectivelyEqual(item.grossArb) &&
                item.netArb !== null;
              return (
                <tr key={item.id}>
                  <td className="row-title">
                    {item.eventLabel}
                    <div className="muted">{item.marketLabel}{item.competition ? ` · ${item.competition}` : ""}</div>
                  </td>
                  <td>{item.status}</td>
                  <td>{percent(item.grossArb)}</td>
                  <td className={belowEven ? "edge-negative" : item.netArb !== null && item.netArb > 0 ? "edge" : ""}>
                    {percent(item.netArb)}
                    {belowEven ? " · below break-even" : ""}
                  </td>
                  <td>{percent(item.trigger)}</td>
                  <td>{item.status === "TRIGGERED" ? "triggered" : percentPoints(item.distanceToTriggerPp)}</td>
                  <td>{percentPoints(comparison)}</td>
                  <td className="muted">
                    Discovery {item.discoverySource ?? "matchbook"}
                    {item.venues.length ? ` · quotes ${item.venues.join("/")}` : ""}
                    <div>last updated {item.scannedAt ? new Date(item.scannedAt).toISOString() : "—"}</div>
                    <div>age at last evaluation {item.quoteFreshness ?? "—"}</div>
                    {item.fixtureStatus ? <div>Matchbook status {item.fixtureStatus}</div> : null}
                    {item.inRunning ? <div>in-running (Matchbook flag)</div> : null}
                    <div>score {item.liveScoreLabel ?? "unavailable"}</div>
                  </td>
                  <td className="muted">
                    {item.strikeNarrative === "approaching"
                      ? "Approaching threshold"
                      : item.strikeNarrative === "moving_away"
                        ? "Moving away from threshold"
                        : item.strikeNarrative === "stable"
                          ? "Distance unchanged vs prior observation"
                          : "Insufficient observation history"}
                    <div>
                      {item.observationCount ?? 0} stored observation
                      {(item.observationCount ?? 0) === 1 ? "" : "s"} · sequence only, not causation
                    </div>
                  </td>
                  <td className="muted">
                    {feeVisible
                      ? "Gross prices are effectively equal; fees/costs make net margin negative."
                      : item.executable
                        ? "Solver-owned triggered economics."
                        : "Backend current_net_edge vs selected comparison threshold only."}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
