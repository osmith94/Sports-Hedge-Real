import Link from "next/link";

import { ActivityEvent } from "../lib/arbitrage-ops";
import { activityHistoryPath } from "../lib/watchlist";
import { HydratedRelativeTime } from "./hydrated-relative-time";

export function ActivityFeed({
  items,
  usedFixture,
}: {
  items: ActivityEvent[];
  usedFixture: boolean;
}) {
  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Activity feed</div>
          <div className="panel-meta">
            PAPER operator timeline · Qualifying opportunity, Promoted to HOT, paper eligible, trigger lost, trade entered, trade exited
          </div>
        </div>
          <span className={usedFixture ? "demo-chip" : "status-badge"}>
          {usedFixture ? "DEMO / FIXTURE" : "PAPER LIVE"}
        </span>
      </div>
      <div className="panel-body feed-list">
        {items.length === 0 ? (
          <div className="empty-live-compact">No operator-significant activity yet.</div>
        ) : items.map((item) => (
          <div
            className="feed-row"
            key={item.id}
            data-event-type={item.eventType}
            data-opportunity-id={item.opportunityId}
            data-missed-trigger-event-id={item.missedTriggerEventId ?? undefined}
            data-fixture-label={item.fixtureLabel ?? undefined}
            data-market-family={item.marketFamily ?? undefined}
            data-canonical-event-id={item.canonicalEventId ?? undefined}
          >
            <div className="feed-kind">{item.kind.replaceAll("_", " ")}</div>
            <div className="feed-body">
              <div className="feed-title">
                {item.title}
                {item.provenance === "DEMO_FIXTURE" ? <span className="demo-inline">DEMO</span> : null}
                {item.opportunityId ? (
                  <Link
                    className="feed-history"
                    href={activityHistoryPath(item.opportunityId, item.canonicalEventId)}
                    data-history-opportunity-id={item.opportunityId}
                    data-history-canonical-event-id={item.canonicalEventId ?? undefined}
                  >
                    History
                  </Link>
                ) : null}
              </div>
              {item.subject ? <div className="feed-subject">{item.subject}</div> : null}
              {item.detail ? <div className="feed-detail">{item.detail}</div> : null}
            </div>
            <div className="feed-time">
              <HydratedRelativeTime iso={item.at} />
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
