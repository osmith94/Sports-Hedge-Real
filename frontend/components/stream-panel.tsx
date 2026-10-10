"use client";

import { useEffect, useState } from "react";

import { StreamStatus, getStreamStatus, pauseStream, removeStreamFixture, resumeStream } from "../lib/api";
import {
  STREAM_HEADING,
  STREAM_HELP,
  STREAM_SHADOW_LABEL,
  formatStreamDuration,
  streamCandidateSummary,
  streamDataLabel,
  streamEconomicsLabel,
  streamFixtureName,
  streamPrice2Label,
  streamRibbon,
  streamStatusLabel,
  streamStatusTone,
  streamTopReason,
} from "../lib/stream-status-display";

type StreamPanelViewProps = {
  status: StreamStatus | null;
  error: string | null;
  busy: boolean;
  onPause: () => void;
  onResume: () => void;
  onRemove: () => void;
};

export function StreamPanelView({
  status,
  error,
  busy,
  onPause,
  onResume,
  onRemove,
}: StreamPanelViewProps) {
  const selected = Boolean(status?.canonical_event_id);
  const tone = streamStatusTone(status);
  const ribbon = streamRibbon(status);
  const candidates = status?.candidates ?? [];
  const executionOff = status?.execution_enabled !== true;

  return (
    <section className="panel stream-panel">
      <div className="stream-panel-top">
        <div className="stream-panel-identity">
          <div className="stream-panel-kicker">{STREAM_HEADING}</div>
          <h2 className="stream-panel-fixture">{streamFixtureName(status)}</h2>
          {status?.competition ? <p className="stream-panel-comp">{status.competition}</p> : null}
        </div>
        <div className="stream-panel-flags">
          <span className={tone === "ok" ? "status-badge" : `status-badge status-badge-${tone}`}>
            {streamStatusLabel(status)}
          </span>
          {executionOff ? (
            <span className="status-badge status-badge-warn stream-execution-off">{STREAM_SHADOW_LABEL}</span>
          ) : null}
        </div>
        <div className="stream-actions">
          <button type="button" disabled={busy || !selected || Boolean(status?.paused)} onClick={onPause}>
            Pause
          </button>
          <button type="button" disabled={busy || !status?.paused} onClick={onResume}>
            Resume
          </button>
          <button type="button" disabled={busy || !selected} onClick={onRemove}>
            Remove
          </button>
        </div>
      </div>
      {selected ? (
        <ul className="stream-ribbon" aria-label="STREAM session metrics">
          {ribbon.map((item) => (
            <li key={item.key} className={`stream-ribbon-item tone-${item.tone || "muted"}`}>
              <span className="stream-ribbon-label">{item.label}</span>
              <span className="stream-ribbon-value">{item.value}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {status?.books_current === false && selected ? (
        <p className="stream-panel-note muted">
          {status.paused
            ? "Paused. Book counts are not current. Session counters retained."
            : "Books are not a populated live snapshot. An active subscription is not a filled book."}
        </p>
      ) : null}
      <p className="stream-panel-summary">{streamCandidateSummary(status)}</p>
      {candidates.length ? (
        <div className="stream-market-table-wrap">
          <table className="stream-market-table">
            <caption className="visually-hidden">Pinned STREAM markets</caption>
            <thead>
              <tr>
                <th scope="col">Market</th>
                <th scope="col">Data</th>
                <th scope="col">Economics</th>
                <th scope="col">Price-2</th>
                <th scope="col">Net</th>
                <th scope="col">Age / skew</th>
                <th scope="col">Reason</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((row) => (
                <tr key={row.catalogue_row_id} className={row.current === false ? "is-cached" : undefined}>
                  <th scope="row">{row.register_canonical_key}</th>
                  <td>{streamDataLabel(row)}</td>
                  <td>{streamEconomicsLabel(row)}</td>
                  <td>{streamPrice2Label(row)}</td>
                  <td>{row.net_edge && streamEconomicsLabel(row) !== "blocked" ? row.net_edge : "—"}</td>
                  <td>
                    {formatStreamDuration(row.pair_age_ms)} / {formatStreamDuration(row.skew_ms)}
                  </td>
                  <td>
                    <details>
                      <summary>{streamTopReason(row)}</summary>
                      <ul>
                        {(row.rejection_reasons.length ? row.rejection_reasons : ["none"]).map((reason) => (
                          <li key={reason}>{reason}</li>
                        ))}
                      </ul>
                    </details>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {status?.last_error ? <p className="stream-panel-error">{status.last_error}</p> : null}
      {error ? <p className="stream-panel-error">{error}</p> : null}
      <details className="stream-diagnostics">
        <summary>Details / Diagnostics</summary>
        <dl className="stream-diagnostics-grid">
          <div>
            <dt>Reconnects</dt>
            <dd>{status?.reconnect_count ?? 0}</dd>
          </div>
          <div>
            <dt>Bad messages</dt>
            <dd>{status?.error_bad_message_count ?? 0}</dd>
          </div>
          <div>
            <dt>Unknown tokens</dt>
            <dd>{status?.unknown_token_count ?? 0}</dd>
          </div>
          <div>
            <dt>Rate limits</dt>
            <dd>{status?.matchbook_rate_limited_count ?? 0}</dd>
          </div>
          <div>
            <dt>Coalesced</dt>
            <dd>{status?.coalesced_event_count ?? 0}</dd>
          </div>
          <div>
            <dt>Suppressed</dt>
            <dd>{status?.suppressed_event_count ?? 0}</dd>
          </div>
          <div>
            <dt>Dropped</dt>
            <dd>{status?.dropped_event_count ?? 0}</dd>
          </div>
          <div>
            <dt>Last trigger</dt>
            <dd>{status?.last_trigger_reason || "none"}</dd>
          </div>
          <div>
            <dt>Last snapshot</dt>
            <dd>{status?.last_full_snapshot_at || "—"}</dd>
          </div>
          <div>
            <dt>Last incremental</dt>
            <dd>{status?.last_incremental_update_at || "—"}</dd>
          </div>
          <div>
            <dt>Venue orders</dt>
            <dd>{status?.orders_placed ? "recorded" : "none"}</dd>
          </div>
        </dl>
        {status?.limitations?.length ? (
          <ul className="stream-limitations">
            {status.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        ) : (
          <p className="muted">{STREAM_HELP}</p>
        )}
      </details>
    </section>
  );
}

export function StreamPanel() {
  const [status, setStatus] = useState<StreamStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const poll = () => {
      void getStreamStatus()
        .then((next) => {
          if (!cancelled) {
            setStatus(next);
            setError(null);
          }
        })
        .catch(() => {
          if (!cancelled) setError("STREAM status unavailable. Nothing was fabricated.");
        });
    };
    poll();
    const handle = window.setInterval(poll, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(handle);
    };
  }, []);

  async function run(action: () => Promise<StreamStatus>) {
    setBusy(true);
    try {
      setStatus(await action());
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "STREAM action failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <StreamPanelView
      status={status}
      error={error}
      busy={busy}
      onPause={() => void run(pauseStream)}
      onResume={() => void run(resumeStream)}
      onRemove={() => void run(removeStreamFixture)}
    />
  );
}
