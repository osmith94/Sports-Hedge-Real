import Link from "next/link";

import { HydratedRelativeTime } from "../../../components/hydrated-relative-time";
import { getWatchlistActivity, OpportunityLifecycleEvent } from "../../../lib/api";
import { percent, percentPoints } from "../../../lib/format";
import {
  noFillHistorySummary,
  sortLifecycleChronological,
} from "../../../lib/opportunity-history-display";
import { activitySubjectFromEvent, lifecycleEventTitle } from "../../../lib/watchlist";

export const dynamic = "force-dynamic";

function captureEligibleLabel(value: boolean | null | undefined): string {
  if (value === true) return "true";
  if (value === false) return "false";
  return "—";
}

export default async function OpportunityActivityHistoryPage({
  params,
}: {
  params: Promise<{ opportunityId: string }>;
}) {
  const { opportunityId: rawId } = await params;
  const opportunityId = decodeURIComponent(rawId);
  let events: OpportunityLifecycleEvent[] = [];
  let historyAvailable = true;
  try {
    events = await getWatchlistActivity(
      `limit=1000&opportunity_id=${encodeURIComponent(opportunityId)}`,
    );
  } catch {
    historyAvailable = false;
  }

  const chronological = sortLifecycleChronological(events);
  const summary = noFillHistorySummary(chronological);
  const subject = chronological.map(activitySubjectFromEvent).find(Boolean) ?? null;

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Activity history</div>
          <h1>{subject ?? opportunityId}</h1>
          <p className="page-subtitle">
            PAPER MODE persisted lifecycle for this opportunity. Unfiltered append-only
            audit, including events hidden from the primary Activity feed. No venue orders
            are placed. Historical rows created before Paper eligible will not have a
            retroactive eligible event.
          </p>
        </div>
        <Link href="/" className="demo-label">
          Back to Activity
        </Link>
      </div>
      {summary ? (
        <div className="value-overlay" style={{ marginBottom: 14 }}>
          <div className="value-overlay-kicker">No-fill summary</div>
          <div className="value-overlay-row">{summary}</div>
          <p className="value-overlay-note">
            Derived only from persisted lifecycle events. Not inferred from elapsed time or
            current watchlist status.
          </p>
        </div>
      ) : null}
      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Persisted lifecycle</div>
            <div className="panel-meta">
              Opportunity {opportunityId}
              {historyAvailable ? "" : " · watchlist history unavailable"}
            </div>
          </div>
          <span className="status-badge">PAPER LIVE</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Event</th>
                <th>Status</th>
                <th>Fixture / market</th>
                <th>Net edge</th>
                <th>Distance</th>
                <th>Capture eligible</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {!historyAvailable ? (
                <tr>
                  <td colSpan={8}>Watchlist history could not be loaded.</td>
                </tr>
              ) : chronological.length === 0 ? (
                <tr>
                  <td colSpan={8}>No persisted lifecycle events for this opportunity.</td>
                </tr>
              ) : (
                chronological.map((event) => (
                  <tr key={event.event_id} data-event-type={event.event_type}>
                    <td>
                      <HydratedRelativeTime iso={event.occurred_at} />
                    </td>
                    <td>{lifecycleEventTitle(event.event_type)}</td>
                    <td>{event.status}</td>
                    <td>{activitySubjectFromEvent(event) ?? "—"}</td>
                    <td>{percent(event.current_net_edge)}</td>
                    <td>{percentPoints(event.distance_to_trigger_pp)}</td>
                    <td>{captureEligibleLabel(event.capture_eligible)}</td>
                    <td>{event.detail?.trim() ? event.detail : "—"}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
