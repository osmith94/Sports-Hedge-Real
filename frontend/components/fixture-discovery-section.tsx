import { LiveRefreshStatus } from "../lib/api";
import {
  discoveryCompactSummaryLabel,
  discoveryStatusBadgeLabel,
} from "../lib/discovered-fixture-display";
import { DiscoveredFixturesPanel } from "./discovered-fixtures";

export function FixtureDiscoverySection({
  status,
  available,
}: {
  status: LiveRefreshStatus | null;
  available: boolean;
}) {
  const summary = discoveryCompactSummaryLabel(status, available);
  const badge = discoveryStatusBadgeLabel(available);

  return (
    <section className="ops-section ops-section-compact">
      <details className="discovery-disclosure">
        <summary className="discovery-summary">
          <span className="discovery-chevron" aria-hidden="true" />
          <span className="discovery-summary-copy">{summary}</span>
          <span className={available ? "status-badge" : "demo-chip"}>{badge}</span>
          <span className="discovery-toggle">
            <span className="discovery-toggle-show">Show discovery</span>
            <span className="discovery-toggle-hide">Hide discovery</span>
          </span>
        </summary>
        <DiscoveredFixturesPanel status={status} available={available} />
      </details>
    </section>
  );
}
