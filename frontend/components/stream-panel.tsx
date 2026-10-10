"use client";

import { useEffect, useState } from "react";

import { StreamStatus, getStreamStatus, pauseStream, removeStreamFixture, resumeStream } from "../lib/api";
import {
  STREAM_COPY,
  STREAM_HEADING,
  STREAM_KICKER,
  STREAM_SHADOW_LABEL,
  streamCandidateSummary,
  streamFixtureName,
  streamStatusLabel,
} from "../lib/stream-status-display";

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
    <section className="panel stream-panel">
      <div className="panel-header">
        <div>
          <div className="hot-zone-kicker">{STREAM_KICKER}</div>
          <div className="panel-title">{STREAM_HEADING}</div>
          <div className="panel-meta">{STREAM_COPY}</div>
        </div>
        <span className="status-badge">{streamStatusLabel(status)}</span>
      </div>
      <p className="section-copy">{STREAM_SHADOW_LABEL}</p>
      <p className="section-copy">{streamFixtureName(status)}</p>
      {status?.canonical_event_id ? (
        <p className="section-copy muted">
          {status.subscribed_market_count}/{status.registered_market_count} registered markets ·{" "}
          {status.token_id_count} token IDs · reconnects {status.reconnect_count} · MB requests{" "}
          {status.matchbook_request_count} · coalesced {status.coalesced_event_count} · dropped{" "}
          {status.dropped_event_count} · 429s {status.matchbook_rate_limited_count} · bad messages{" "}
          {status.error_bad_message_count}
        </p>
      ) : null}
      <p className="section-copy">{streamCandidateSummary(status)}</p>
      {status?.last_error ? <p className="section-copy">{status.last_error}</p> : null}
      {error ? <p className="section-copy">{error}</p> : null}
      <div className="stream-actions">
        <button type="button" disabled={busy || !status?.canonical_event_id || status.paused} onClick={() => void run(pauseStream)}>
          Pause
        </button>
        <button type="button" disabled={busy || !status?.paused} onClick={() => void run(resumeStream)}>
          Resume
        </button>
        <button type="button" disabled={busy || !status?.canonical_event_id} onClick={() => void run(removeStreamFixture)}>
          Remove
        </button>
      </div>
    </section>
  );
}
