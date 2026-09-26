"use client";

import { useLiveStatusOptional } from "./live-status-provider";
import { DiscoveredFixturesPanel } from "./discovered-fixtures";

export function FixtureDiscoverySection() {
  const live = useLiveStatusOptional();
  const catalogue = live?.catalogue ?? null;
  const available = Boolean(catalogue) || Boolean(live && !live.settled);
  const count = catalogue?.universe_fixture_count ?? catalogue?.fixtures.length ?? 0;
  const summary = catalogue
    ? `Universe catalogue · ${count} fixtures · version ${catalogue.universe_catalogue_version.slice(0, 8)}`
    : live && !live.settled
      ? "Universe catalogue · loading"
      : "Universe catalogue · unavailable";
  const badge = catalogue ? "UNIVERSE CATALOGUE" : available ? "LOADING" : "UNAVAILABLE";

  return (
    <section className="ops-section ops-section-compact">
      <details className="discovery-disclosure">
        <summary className="discovery-summary">
          <span className="discovery-chevron" aria-hidden="true" />
          <span className="discovery-summary-copy">{summary}</span>
          <span className={catalogue ? "status-badge" : "demo-chip"}>{badge}</span>
          <span className="discovery-toggle">
            <span className="discovery-toggle-show">Show discovery</span>
            <span className="discovery-toggle-hide">Hide discovery</span>
          </span>
        </summary>
        <DiscoveredFixturesPanel
          fixtures={catalogue?.fixtures ?? []}
          available={Boolean(catalogue) || Boolean(live && !live.settled)}
          configWarnings={live?.status?.config_warnings ?? []}
          lastError={live?.status?.last_error}
        />
      </details>
    </section>
  );
}
