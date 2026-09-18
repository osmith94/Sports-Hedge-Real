import Link from "next/link";

import { FixtureDetailReadModel, PreparablePaperOpportunity } from "../lib/api";
import {
  fixturePhaseLabel,
  kickoffClockLabel,
  marketEvaluationLabel,
  marketEvaluationUnevaluated,
} from "../lib/discovered-fixture-display";
import {
  KalshiFixtureMarketInventoryRow,
  provenanceLines,
} from "../lib/fixture-inventory-display";
import {
  InventoryCardView,
  decisionBadgeClass,
  inventoryCardViewModel,
  toneClass,
} from "../lib/fixture-inventory-operator";
import { PaperDeploymentPreview } from "./paper-deployment-preview";
import { coverageRowLabel, fixtureCoverageRows } from "../lib/catalogue-coverage-display";

type KalshiFixtureDetailReadModel = Omit<FixtureDetailReadModel, "fixture" | "markets"> & {
  fixture: FixtureDetailReadModel["fixture"] & { kalshi_matched?: boolean };
  markets: KalshiFixtureMarketInventoryRow[];
};

export function FixtureInventoryWorkspace({
  detail,
  focusOpportunityId,
}: {
  detail: KalshiFixtureDetailReadModel;
  focusOpportunityId?: string | null;
}) {
  const fixture = detail.fixture;
  const phase = fixturePhaseLabel(fixture);
  const preparable = detail.preparable_opportunities ?? [];

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
      {fixtureCoverageRows(fixture).length ? (
        <ul className="catalogue-coverage-list">
          {fixtureCoverageRows(fixture).map((row) => (
            <li key={row.archetype}>{coverageRowLabel(row)}</li>
          ))}
        </ul>
      ) : null}
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

      <PaperDeploymentPreview opportunities={preparable} focusOpportunityId={focusOpportunityId} />

      {detail.markets.length === 0 ? (
        <div className="empty-live">
          No discovered markets on the last collection for this fixture. Empty stays empty.
        </div>
      ) : (
        <div className="inventory-stack">
          {detail.markets.map((row) => (
            <InventoryRowCard
              key={`${row.display_name}-${row.comparison_status}-${row.matchbook?.source_market_id ?? ""}-${row.polymarket?.source_market_id ?? ""}-${row.kalshi?.source_market_id ?? ""}`}
              row={row}
              preparable={preparable}
            />
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

function InventoryRowCard({
  row,
  preparable,
}: {
  row: KalshiFixtureMarketInventoryRow;
  preparable: PreparablePaperOpportunity[];
}) {
  const view = inventoryCardViewModel(row, preparable);
  return (
    <article
      className={
        view.decision.status === "paper_eligible" ? "opp-card opp-card-hot inventory-card" : "opp-card inventory-card"
      }
    >
      <div className="opp-card-top">
        <div>
          <div className="eyebrow">{view.eyebrow}</div>
          <div className="opp-event">{view.title}</div>
        </div>
        <span className={decisionBadgeClass(view.decision.tone)}>{view.decision.label}</span>
      </div>
      {view.economics ? (
        <p className={`inventory-economics ${toneClass(view.economics.tone)}`}>{view.economics.text}</p>
      ) : null}
      {view.comparableHeadline ? <p className="inventory-pair-headline">{view.comparableHeadline}</p> : null}
      {view.pairBadges.length ? (
        <div className="inventory-pair-row">
          {view.pairBadges.map((badge) => (
            <span key={badge.text} className={`inventory-pair-badge ${toneClass(badge.tone)}`}>
              {badge.text}
            </span>
          ))}
        </div>
      ) : null}
      {view.discoveredNotes.map((note) => (
        <p key={note} className="inventory-discovered">
          {note}
        </p>
      ))}
      <div className="inventory-grid">
        {view.venues.map((venue) => (
          <VenueMiniCardView key={venue.venue} venue={venue} />
        ))}
      </div>
      {view.operatorReason ? <p className="inventory-reason-detail">{view.operatorReason}</p> : null}
      <PaperAction view={view} />
      <details className="scan-advanced inventory-advanced">
        <summary>Advanced · provenance</summary>
        <ProvenanceBlock row={row} />
      </details>
    </article>
  );
}

function VenueMiniCardView({ venue }: { venue: InventoryCardView["venues"][number] }) {
  return (
    <div className="inventory-venue-card">
      <div className="metric-label">
        {venue.name}
        {venue.kind ? ` · ${venue.kind}` : ""}
      </div>
      {venue.present ? (
        <>
          {venue.quotes.map((quote) => (
            <div key={quote} className="inventory-venue-quote">
              {quote}
            </div>
          ))}
          {venue.meta ? <div className="muted">{venue.meta}</div> : null}
          {venue.incompatibility ? (
            <div className="inventory-incompatible">{venue.incompatibility}</div>
          ) : null}
          {venue.failingChecks.map((check) => (
            <div key={check} className="inventory-failing">
              {check}
            </div>
          ))}
        </>
      ) : (
        <div className="muted">not present</div>
      )}
    </div>
  );
}

function PaperAction({ view }: { view: InventoryCardView }) {
  if (view.paperAction.eligible && view.paperAction.href) {
    return (
      <p className="inventory-action">
        <a className="inventory-cta" href={view.paperAction.href}>
          {view.paperAction.label}
        </a>
      </p>
    );
  }
  return <p className="inventory-ineligible">{view.paperAction.label}</p>;
}

function ProvenanceBlock({ row }: { row: KalshiFixtureMarketInventoryRow }) {
  const venues: Array<[string, KalshiFixtureMarketInventoryRow["matchbook"]]> = [
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
      {row.comparison_status ? <p>Mapping status: {row.comparison_status}</p> : null}
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
