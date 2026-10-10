import Link from "next/link";

import { ActivityEvent, ActivityPrice2 } from "../lib/arbitrage-ops";
import { venueShortLabel } from "../lib/fixture-inventory-display";
import { nativeStake } from "../lib/format";
import { activityHistoryPath } from "../lib/watchlist";
import { HydratedLocalClock, HydratedRelativeTime } from "./hydrated-relative-time";
import { Venue } from "../lib/api";

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
            PAPER operator timeline · Qualifying opportunity, radar expired, Promoted to HOT, paper eligible, trigger lost, trade entered, trade exited, Price-2 attempts
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
            data-price2-status={item.price2?.status}
            data-price2-snapshot-id={item.price2?.snapshotId ?? undefined}
            data-price2-cycle={item.price2?.executionCycle ?? undefined}
            data-price2-filled={item.price2 ? String(item.price2.filled) : undefined}
            data-price2-trade-linked={item.price2 ? String(item.price2.tradeLinked) : undefined}
            data-data-kind={item.price2?.dataKind}
          >
            <div className="feed-kind">{item.kind.replaceAll("_", " ")}</div>
            <div className="feed-body">
              <div className="feed-title">
                {item.title}
                {item.provenance === "DEMO_FIXTURE" ? <span className="demo-inline">DEMO</span> : null}
                {item.price2 ? <span className="live-inline">RECORDED AUDIT</span> : null}
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
              {item.price2 ? <Price2Timing item={item.price2} /> : null}
              {item.price2 ? <Price2Detail price2={item.price2} /> : null}
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

function Price2Timing({ item }: { item: ActivityPrice2 }) {
  return (
    <div className="feed-detail feed-price2-timing" data-price2-timing="true">
      <Price2TimingHydrated item={item} />
    </div>
  );
}

function Price2TimingHydrated({ item }: { item: ActivityPrice2 }) {
  return (
    <>
      Quote evaluation {item.startedAt ? <HydratedLocalClock iso={item.startedAt} /> : "start not recorded"}
      {" → "}
      {item.finishedAt ? <HydratedLocalClock iso={item.finishedAt} /> : "end not recorded"}
      {" · "}
      {item.elapsedMs == null
        ? "quote evaluation duration not recorded"
        : `${item.elapsedMs} ms (not order-submit latency)`}
    </>
  );
}

function Price2Detail({ price2 }: { price2: ActivityPrice2 }) {
  const missingSnapshot = price2.source === "lifecycle_rejection";
  return (
    <details className="feed-price2-detail">
      <summary>Price-2 quote detail</summary>
      {missingSnapshot ? (
        <div className="feed-detail">
          No stored Price-2 snapshot. Reason {price2.rejectionReason || "not recorded"}. Odds, size, and elapsed time were not recorded.
        </div>
      ) : (
        <div className="feed-price2-legs">
          {price2.legs.length === 0 ? (
            <div className="feed-detail">Leg quotes not recorded on this older audit row.</div>
          ) : (
            price2.legs.map((leg, index) => (
              <div className="feed-price2-leg" key={`${leg.venue ?? "venue"}-${leg.outcome ?? index}`}>
                <strong>{legVenue(leg.venue)}</strong>
                {" · "}
                {leg.outcome || "outcome not recorded"}
                {" · odds "}
                {leg.displayedOdds || "not recorded"}
                {" · stake "}
                {leg.requestedStake
                  ? nativeStake(leg.requestedStake, leg.stakeCurrency)
                  : "not recorded"}
                {" · retrieved "}
                {leg.retrievedAt ? <HydratedLocalClock iso={leg.retrievedAt} /> : "not recorded"}
                {leg.quoteAgeMs != null ? ` · quote age ${leg.quoteAgeMs}ms` : " · quote age not recorded"}
                {leg.timingMatch === "native_id" && leg.slotWaitMs != null
                  ? ` · matched slot wait ${leg.slotWaitMs}ms`
                  : ""}
                {leg.timingMatch === "native_id" && leg.ioMs != null
                  ? ` · matched I/O ${leg.ioMs}ms`
                  : ""}
                {" · "}
                {freezeLine(leg)}
              </div>
            ))
          )}
          {price2.minimumNetEdge != null && price2.minimumNetEdge !== "" ? (
            <div className="feed-detail">
              Configured minimum net edge {price2.minimumNetEdge}
              {price2.economicsVsThreshold === "below_configured_threshold"
                ? " · economics below threshold (not fill-eligible on edge alone)"
                : price2.economicsVsThreshold === "meets_or_exceeds_configured_threshold"
                  ? " · economics met threshold; native-order proof is separate"
                  : ""}
            </div>
          ) : (
            <div className="feed-detail">Configured minimum net edge not recorded on this audit.</div>
          )}
          {price2.venueTimings.length ? (
            <div className="feed-price2-venue-timing">
              {price2.venueTimings.map((timing) => (
                <div key={timing.venue}>
                  {legVenue(timing.venue)} provider timing (venue-aggregated max of {timing.callCount} call
                  {timing.callCount === 1 ? "" : "s"}, not per-leg)
                  {timing.slotWaitMs != null ? ` · slot wait ${timing.slotWaitMs}ms` : " · slot wait not recorded"}
                  {timing.ioMs != null ? ` · I/O ${timing.ioMs}ms` : " · I/O not recorded"}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      )}
    </details>
  );
}

function freezeLine(leg: ActivityPrice2["legs"][number]): string {
  if (leg.freezeStatus === "frozen") return "native order frozen";
  if (leg.freezeStatus === "not_frozen") {
    const reason = (leg.freezeReason || "unknown/unrecorded").replaceAll("_", " ");
    const extras = [
      leg.observedTickSize ? `tick ${leg.observedTickSize}` : null,
      leg.observedMinimumShares ? `min shares ${leg.observedMinimumShares}` : null,
      leg.intendedNativeShares ? `intended shares ${leg.intendedNativeShares}` : null,
      leg.intendedLimitPrice ? `intended price ${leg.intendedLimitPrice}` : null,
    ].filter(Boolean);
    const suffix = extras.length ? ` (${extras.join(", ")})` : "";
    return `native order not proved: ${reason}${suffix}`;
  }
  return "native order freeze details not recorded";
}

function legVenue(venue: string | null): string {
  if (!venue) return "venue not recorded";
  return venueShortLabel(venue as Venue);
}
