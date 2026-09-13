import Link from "next/link";

import { FixtureDetailReadModel, VenueMarketFacts } from "../lib/api";
import {
  fixturePhaseLabel,
  kickoffClockLabel,
  marketEvaluationLabel,
  marketEvaluationUnevaluated,
} from "../lib/discovered-fixture-display";
import {
  KalshiFixtureMarketInventoryRow,
  comparisonLabel,
  coverageLabel,
  economicsSummary,
  humanizeToken,
  limitingVenues,
  mismatchExplanation,
  pairSummary,
  provenanceLines,
  quoteSummary,
  reasonLabel,
  settlementLabel,
  solverFacts,
} from "../lib/fixture-inventory-display";
import { PaperDeploymentPreview } from "./paper-deployment-preview";

type KalshiFixtureDetailReadModel = Omit<FixtureDetailReadModel, "fixture" | "markets"> & {
  fixture: FixtureDetailReadModel["fixture"] & { kalshi_matched?: boolean };
  markets: KalshiFixtureMarketInventoryRow[];
};

export function FixtureInventoryWorkspace({ detail }: { detail: KalshiFixtureDetailReadModel }) {
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
            Every discovered market stays visible, including unsupported and rejected rows.
            Solver edge is shown only when the existing paper scan produced it.
          </p>
        </div>
      </div>

      <p className="section-copy">
        {marketEvaluationUnevaluated(fixture)
          ? marketEvaluationLabel(fixture)
          : `${fixture.discovered_market_count ?? fixture.matched_market_count} discovered · ${
              fixture.matched_equivalent_count ?? 0
            } matched equivalent`}
        {fixture.current_net_edge != null
          ? ` · best calculated net edge from solver rows only`
          : " · no solver net edge"}
        . Matchbook status {fixture.fixture_status ?? "unknown"}
        {fixture.in_running ? " · in-play" : " · pre-match"}. Polymarket{" "}
        {fixture.polymarket_matched ? "fixture found" : fixture.no_comparison_reason ?? "unmatched"}
        {fixture.kalshi_matched != null
          ? `. Kalshi ${fixture.kalshi_matched ? "fixture found" : "unmatched"}`
          : ""}
        .
      </p>
      {detail.paper_entries?.length ? (
        <p className="section-copy">
          Paper entries:{" "}
          {detail.paper_entries
            .map((entry) => {
              const kinds = entry.fill_kinds.join("/");
              const model = entry.solver_model ?? "solver";
              return `${entry.state} ${model} (${kinds || "no fills"})`;
            })
            .join(" · ")}
          . OPEN only after complete validated paper entry. PAPER MODE · execution disabled.
        </p>
      ) : null}

      <PaperDeploymentPreview opportunities={detail.preparable_opportunities ?? []} />

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

      <details className="scan-advanced inventory-advanced">
        <summary>Advanced · fixture provenance</summary>
        <p className="inventory-advanced-copy">Canonical event {fixture.canonical_event_id}</p>
      </details>
    </>
  );
}

function InventoryRowCard({ row }: { row: KalshiFixtureMarketInventoryRow }) {
  const limiting = limitingVenues(row);
  const explanation = mismatchExplanation(row);
  const family = row.family ? humanizeToken(row.family) : "";
  const period = row.period ? humanizeToken(row.period) : "";

  return (
    <article className="opp-card">
      <div className="opp-card-top">
        <div>
          <div className="opp-event">{row.display_name}</div>
          <div className="muted">
            {coverageLabel(row)} · {comparisonLabel(row.comparison_status)}
            {family ? ` · ${family}` : ""}
            {period ? ` · ${period}` : ""}
          </div>
        </div>
        <span className="status-badge">{comparisonLabel(row.comparison_status)}</span>
      </div>
      <div className="inventory-grid">
        <VenueColumn name="Matchbook" facts={row.matchbook} limiting={limiting.has("matchbook")} />
        <VenueColumn name="Polymarket" facts={row.polymarket} limiting={limiting.has("polymarket")} />
        <VenueColumn name="Kalshi" facts={row.kalshi} limiting={limiting.has("kalshi")} />
      </div>
      <p className="section-copy">{solverFacts(row)}</p>
      {row.pair_results?.length ? (
        <p className="muted">
          Pairwise: {row.pair_results.map((pair) => pairSummary(pair)).join(" · ")}
        </p>
      ) : null}
      {row.rejection_reasons.length ? (
        <p className="muted">
          Reasons: {row.rejection_reasons.map((reason) => reasonLabel(reason)).join(" · ")}
        </p>
      ) : null}
      {explanation ? <p className="inventory-reason-detail">{explanation}</p> : null}
      <details className="scan-advanced inventory-advanced">
        <summary>Advanced · provenance</summary>
        <ProvenanceBlock row={row} />
      </details>
    </article>
  );
}

function VenueColumn({
  name,
  facts,
  limiting,
}: {
  name: string;
  facts: VenueMarketFacts | null | undefined;
  limiting: boolean;
}) {
  return (
    <div>
      <div className="metric-label">{name}</div>
      {facts ? (
        <>
          <div>{quoteSummary(facts)}</div>
          <div className="muted">{economicsSummary(facts, { limiting })}</div>
          <div className="muted">{settlementLabel(facts)}</div>
        </>
      ) : (
        <div className="muted">not present</div>
      )}
    </div>
  );
}

function ProvenanceBlock({ row }: { row: KalshiFixtureMarketInventoryRow }) {
  const venues: Array<[string, VenueMarketFacts | null | undefined]> = [
    ["Matchbook", row.matchbook],
    ["Polymarket", row.polymarket],
    ["Kalshi", row.kalshi],
  ];
  const matched = row.comparison_status === "matched_equivalent";
  const rawReasons = [
    row.reason,
    ...row.rejection_reasons,
    ...row.match_reasons,
    ...(row.pair_results ?? []).flatMap((pair) => pair.rejection_reasons),
  ].filter(
    (reason, index, all): reason is string =>
      Boolean(reason) &&
      all.indexOf(reason) === index &&
      !(matched && reason === "venue_only"),
  );

  return (
    <div className="inventory-advanced-copy">
      {venues.map(([name, facts]) =>
        facts ? (
          <p key={name}>
            {name}: {provenanceLines(facts).join(" · ")}
          </p>
        ) : (
          <p key={name}>{name}: not present</p>
        ),
      )}
      {rawReasons.length ? <p>Raw codes: {rawReasons.join(", ")}</p> : null}
    </div>
  );
}
