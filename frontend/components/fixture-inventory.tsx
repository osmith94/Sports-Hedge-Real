import Link from "next/link";

import {
  FixtureDetailReadModel,
  FixtureMarketInventoryRow,
} from "../lib/api";
import {
  fixturePhaseLabel,
  kickoffClockLabel,
} from "../lib/discovered-fixture-display";
import {
  comparisonLabel,
  coverageLabel,
  economicsSummary,
  quoteSummary,
  solverFacts,
} from "../lib/fixture-inventory-display";

export function FixtureInventoryWorkspace({ detail }: { detail: FixtureDetailReadModel }) {
  const fixture = detail.fixture;
  const phase = fixturePhaseLabel(fixture);

  return (
    <>
      <div className="pa-detail-nav">
        <Link href="/">← Operations console</Link>
        <span className="pa-chip pa-chip-paper">PAPER MODE · NO EXECUTION</span>
        <span className="status-badge">{detail.data_class.replaceAll("_", " ")}</span>
      </div>

      <div className="page-heading">
        <div>
          <div className="eyebrow">Fixture drill-down</div>
          <h1>
            {fixture.home_team} v {fixture.away_team} · {phase} · {kickoffClockLabel(fixture.kickoff_utc)}
          </h1>
          <p className="page-subtitle">
            Canonical event {fixture.canonical_event_id}. Every discovered market stays visible,
            including unsupported and rejected rows. Solver edge is shown only when the existing
            paper scan produced it.
          </p>
        </div>
      </div>

      <p className="section-copy">
        {fixture.discovered_market_count ?? fixture.matched_market_count} discovered ·{" "}
        {fixture.matched_equivalent_count ?? 0} matched equivalent
        {fixture.current_net_edge != null
          ? ` · best calculated net edge from solver rows only`
          : " · no solver net edge"}
        . Matchbook status {fixture.fixture_status ?? "unknown"}
        {fixture.in_running ? " · in-play" : " · pre-match"}. Polymarket{" "}
        {fixture.polymarket_matched ? "matched" : fixture.no_comparison_reason ?? "unmatched"}
        {fixture.kalshi_matched != null
          ? `. Kalshi ${fixture.kalshi_matched ? "matched" : "unmatched"}`
          : ""}
        .
      </p>

      {detail.markets.length === 0 ? (
        <div className="empty-live">
          No discovered markets on the last collection for this fixture. Empty stays empty.
        </div>
      ) : (
        <div className="inventory-stack">
          {detail.markets.map((row) => (
            <InventoryRowCard key={`${row.display_name}-${row.comparison_status}-${row.matchbook?.source_market_id ?? ""}-${row.polymarket?.source_market_id ?? ""}-${row.kalshi?.source_market_id ?? ""}`} row={row} />
          ))}
        </div>
      )}
    </>
  );
}

function InventoryRowCard({ row }: { row: FixtureMarketInventoryRow }) {
  return (
    <article className="opp-card">
      <div className="opp-card-top">
        <div>
          <div className="opp-event">{row.display_name}</div>
          <div className="muted">
            {coverageLabel(row)} · {comparisonLabel(row.comparison_status)}
            {row.family ? ` · ${row.family}` : ""}
            {row.period ? ` · ${row.period}` : ""}
          </div>
        </div>
        <span className="status-badge">{comparisonLabel(row.comparison_status)}</span>
      </div>
      <div className="inventory-grid">
        <div>
          <div className="metric-label">Matchbook</div>
          <div>{quoteSummary(row.matchbook)}</div>
          <div className="muted">{economicsSummary(row.matchbook)}</div>
          <div className="muted">
            {row.matchbook
              ? `id ${row.matchbook.source_market_id} · settlement ${row.matchbook.settlement_complete ? "complete" : "incomplete/unknown"}`
              : "not present"}
          </div>
        </div>
        <div>
          <div className="metric-label">Polymarket</div>
          <div>{quoteSummary(row.polymarket)}</div>
          <div className="muted">{economicsSummary(row.polymarket)}</div>
          <div className="muted">
            {row.polymarket
              ? `id ${row.polymarket.source_market_id} · settlement ${row.polymarket.settlement_complete ? "complete" : "incomplete/unknown"}`
              : "not present"}
          </div>
        </div>
        <div>
          <div className="metric-label">Kalshi</div>
          <div>{quoteSummary(row.kalshi)}</div>
          <div className="muted">{economicsSummary(row.kalshi)}</div>
          <div className="muted">
            {row.kalshi
              ? `id ${row.kalshi.source_market_id} · settlement ${row.kalshi.settlement_complete ? "complete" : "incomplete/unknown"}`
              : "not present"}
          </div>
        </div>
      </div>
      <p className="section-copy">{solverFacts(row)}</p>
      {row.pair_results?.length ? (
        <p className="muted">
          Pairwise:{" "}
          {row.pair_results
            .map((pair) => {
              const label = `${pair.left_venue}↔${pair.right_venue}`;
              if (pair.entered_solver) {
                return `${label} ${pair.solver_model ?? "solver"}`;
              }
              const reason = pair.rejection_reasons[0] ?? "not entered";
              return `${label} ${reason}`;
            })
            .join(" · ")}
        </p>
      ) : null}
      {row.rejection_reasons.length ? (
        <p className="muted">Reasons: {row.rejection_reasons.join(", ")}</p>
      ) : null}
    </article>
  );
}
