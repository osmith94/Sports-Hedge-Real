import type { Price2ActivityObservation } from "../lib/api";
import {
  summarizePrice2Attempts,
  type ReasonCount,
  type TimedSummary,
} from "../lib/price2-performance";

const WINDOW_HOURS = 24;
export const PRICE2_PANEL_SAMPLE_LIMIT = 50;

function milliseconds(value: number | null): string {
  return value == null ? "—" : `${value.toLocaleString("en-GB")} ms`;
}

function timingRange(summary: TimedSummary): string {
  return summary.count
    ? `p50 ${milliseconds(summary.p50)} · p90 ${milliseconds(summary.p90)} (n=${summary.count})`
    : "Not recorded";
}

function ReasonList({ rows, empty }: { rows: ReasonCount[]; empty: string }) {
  if (!rows.length) return <p className="panel-meta">{empty}</p>;
  return (
    <ol className="price2-perf-reasons">
      {rows.slice(0, 5).map((row) => (
        <li key={row.reason}>
          <span>{row.reason.replaceAll("_", " ")}</span>
          <strong>{row.count}</strong>
        </li>
      ))}
    </ol>
  );
}

export function Price2PerformancePanel({
  attempts,
  available,
}: {
  attempts: Price2ActivityObservation[];
  available: boolean;
}) {
  const data = summarizePrice2Attempts(attempts);
  return (
    <section className="panel price2-perf-panel" aria-labelledby="price2-perf-title">
      <div className="panel-header">
        <div>
          <div className="panel-title" id="price2-perf-title">Price-2 performance</div>
          <div className="panel-meta">
            Read-only audit · latest {PRICE2_PANEL_SAMPLE_LIMIT} attempts within {WINDOW_HOURS} hours · not exchange submit latency
          </div>
        </div>
        <span className={available ? "status-badge" : "demo-chip"}>
          {available ? "RECORDED AUDIT" : "UNAVAILABLE"}
        </span>
      </div>
      <div className="panel-body">
        {!available ? (
          <div className="empty-live-compact">Price-2 audit is unavailable. Metrics were not estimated.</div>
        ) : data.attempts === 0 ? (
          <div className="empty-live-compact">No recorded Price-2 attempts in the recent sample.</div>
        ) : (
          <>
            <div className="price2-perf-summary">
              <div><span>Recorded attempts</span><strong>{data.attempts}</strong></div>
              <div><span>Accepted</span><strong>{data.accepted}</strong></div>
              <div><span>Rejected</span><strong>{data.rejected}</strong></div>
              <div><span>Unavailable</span><strong>{data.unavailable}</strong></div>
            </div>
            <div className="price2-perf-facts">
              <div><span>Evaluation duration</span><strong>{timingRange(data.evaluation)}</strong></div>
              <div><span>Retrieval skew</span><strong>{timingRange(data.skew)}</strong></div>
              <div><span>Oldest quote age</span><strong>{timingRange(data.quoteAge)}</strong></div>
            </div>
            <div className="price2-perf-subhead">Provider latency <span>(per-attempt venue max; not summed)</span></div>
            {data.venues.length ? (
              <div className="price2-perf-table-wrap">
                <table className="price2-perf-table">
                  <thead><tr><th>Venue</th><th>Slot wait p50 / p90</th><th>I/O p50 / p90</th></tr></thead>
                  <tbody>
                    {data.venues.map((venue) => (
                      <tr key={venue.venue}>
                        <td>{venue.venue} <span className="panel-meta">(n={venue.attempts})</span></td>
                        <td>{timingRange(venue.wait)}</td>
                        <td>{timingRange(venue.io)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <p className="panel-meta">No provider timings recorded in this sample.</p>}
            <div className="price2-perf-breakdowns">
              <div>
                <div className="price2-perf-subhead">Price-2 rejection reasons</div>
                <ReasonList rows={data.rejectionReasons} empty="No recorded rejections." />
              </div>
              <div>
                <div className="price2-perf-subhead">Polymarket native-order freeze failures</div>
                <ReasonList rows={data.polymarketFreezeReasons} empty="No recorded Polymarket freeze failures." />
              </div>
            </div>
            <div className="panel-meta price2-perf-foot">
              Sample size {data.attempts}/{PRICE2_PANEL_SAMPLE_LIMIT}{data.attempts >= PRICE2_PANEL_SAMPLE_LIMIT ? " (capped; not all attempts)" : ""}.
              {" "}Timing coverage: {data.evaluation.count}/{data.attempts} evaluation, {data.skew.count}/{data.attempts} skew,
              {" "}{data.quoteAge.count}/{data.attempts} quote age. Accepted-but-not-filled: {data.acceptedNotFilled}.
              {" "}Recorded fill flag: {data.recordedFills} (not independently verified exchange settlement).
              {" "}Refresh this page for new records; no provider calls or trading actions.
            </div>
          </>
        )}
      </div>
    </section>
  );
}
