import { ActivityEvent } from "../lib/arbitrage-ops";
import { relativeTime } from "../lib/format";

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
          <div className="panel-meta">Paper watchlist, threshold, fill and rejection events</div>
        </div>
        <span className={usedFixture ? "demo-chip" : "status-badge"}>
          {usedFixture ? "LIVE + DEMO FILL LIFECYCLE" : "SCANNER DATA"}
        </span>
      </div>
      <div className="panel-body feed-list">
        {items.map((item) => (
          <div className="feed-row" key={item.id}>
            <div className="feed-kind">{item.kind.replaceAll("_", " ")}</div>
            <div className="feed-body">
              <div className="feed-title">
                {item.title}
                {item.provenance === "DEMO_FIXTURE" ? <span className="demo-inline">DEMO</span> : null}
              </div>
              <div className="feed-detail">{item.detail}</div>
            </div>
            <div className="feed-time">{relativeTime(item.at)}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
