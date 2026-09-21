import Link from "next/link";

import { HydratedRelativeTime } from "../../../components/hydrated-relative-time";
import { getWatchlistActivity, OpportunityLifecycleEvent } from "../../../lib/api";
import { percent, percentPoints } from "../../../lib/format";
import {
  noFillHistorySummary,
  sortLifecycleChronological,
} from "../../../lib/opportunity-history-display";
import {
  activitySubjectFromEvent,
  attemptIdFromLifecycleEvent,
  canonicalEventIdFromHotOpportunityId,
  lifecycleEventTitle,
} from "../../../lib/watchlist";

export const dynamic = "force-dynamic";

function captureEligibleLabel(value: boolean | null | undefined): string {
  if (value === true) return "true";
  if (value === false) return "false";
  return "—";
}

export default async function OpportunityActivityHistoryPage({
  params,
  searchParams,
}: {
  params: Promise<{ opportunityId: string }>;
  searchParams: Promise<{ canonical_event_id?: string | string[] }>;
}) {
  const { opportunityId: rawId } = await params;
  const query = await searchParams;
  const opportunityId = decodeURIComponent(rawId);
  const rawCanonical = Array.isArray(query.canonical_event_id)
    ? query.canonical_event_id[0]
    : query.canonical_event_id;
  const canonicalEventId =
    rawCanonical?.trim() || canonicalEventIdFromHotOpportunityId(opportunityId);
  let events: OpportunityLifecycleEvent[] = [];
  let historyAvailable = true;
  try {
    const identity = canonicalEventId
      ? `canonical_event_id=${encodeURIComponent(canonicalEventId)}&opportunity_id=${encodeURIComponent(opportunityId)}`
      : `opportunity_id=${encodeURIComponent(opportunityId)}`;
    events = await getWatchlistActivity(`limit=1000&${identity}`);
  } catch {
    historyAvailable = false;
  }

  const chronological = sortLifecycleChronological(events);
  const summary = noFillHistorySummary(chronological, opportunityId);
  const subject = chronological.map(activitySubjectFromEvent).find(Boolean) ?? null;
  const aggregated = Boolean(canonicalEventId);

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Activity history</div>
          <h1>{subject ?? opportunityId}</h1>
          <p className="page-subtitle">
            PAPER MODE persisted lifecycle
            {aggregated
              ? " for this canonical event, including fixture-scoped HOT promotion and later market/opportunity rows that share the same canonical_event_id."
              : " for this opportunity."}{" "}
            Unfiltered append-only audit, including events hidden from the primary Activity
            feed. No venue orders are placed. Historical rows created before Paper eligible
            will not have a retroactive eligible event.
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
            Derived only from persisted lifecycle events in the latest Paper eligible
            episode for this opportunity. Not inferred from elapsed time, current
            watchlist status, or an earlier episode.
          </p>
        </div>
      ) : null}
      <section className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Persisted lifecycle</div>
            <div className="panel-meta">
              Opportunity {opportunityId}
              {canonicalEventId ? ` · canonical event ${canonicalEventId}` : ""}
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
                <th>Canonical event</th>
                <th>Canonical market</th>
                <th>Attempt</th>
                <th>Net edge</th>
                <th>Distance</th>
                <th>Capture eligible</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {!historyAvailable ? (
                <tr>
                  <td colSpan={11}>Watchlist history could not be loaded.</td>
                </tr>
              ) : chronological.length === 0 ? (
                <tr>
                  <td colSpan={11}>No persisted lifecycle events for this opportunity.</td>
                </tr>
              ) : (
                chronological.map((event) => {
                  const attemptId = attemptIdFromLifecycleEvent(event);
                  return (
                    <tr
                      key={event.event_id}
                      data-event-type={event.event_type}
                      data-opportunity-id={event.opportunity_id}
                      data-canonical-event-id={event.canonical_event_id ?? undefined}
                      data-canonical-market-id={event.canonical_market_id ?? undefined}
                      data-attempt-id={attemptId ?? undefined}
                    >
                      <td>
                        <HydratedRelativeTime iso={event.occurred_at} />
                      </td>
                      <td>{lifecycleEventTitle(event.event_type)}</td>
                      <td>{event.status}</td>
                      <td>{activitySubjectFromEvent(event) ?? "—"}</td>
                      <td>{event.canonical_event_id?.trim() ? event.canonical_event_id : "—"}</td>
                      <td>{event.canonical_market_id?.trim() ? event.canonical_market_id : "—"}</td>
                      <td>{attemptId ?? "—"}</td>
                      <td>{percent(event.current_net_edge)}</td>
                      <td>{percentPoints(event.distance_to_trigger_pp)}</td>
                      <td>{captureEligibleLabel(event.capture_eligible)}</td>
                      <td>{event.detail?.trim() ? event.detail : "—"}</td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
