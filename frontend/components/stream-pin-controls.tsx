"use client";

import { useState } from "react";

import { selectStreamFixture } from "../lib/api";
import { SEND_TO_STREAM_LABEL, STREAM_SHADOW_LABEL } from "../lib/stream-status-display";

export function StreamPinControls({ canonicalEventId }: { canonicalEventId: string }) {
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  return (
    <div className="stream-pin-controls">
      <button
        type="button"
        disabled={busy || !canonicalEventId}
        onClick={() => {
          setBusy(true);
          void selectStreamFixture(canonicalEventId)
            .then((status) => {
              setMessage(
                status.canonical_event_id
                  ? `STREAM ${status.connection_status.replaceAll("_", " ")}. ${STREAM_SHADOW_LABEL}`
                  : "STREAM did not select this fixture.",
              );
            })
            .catch((error: unknown) => {
              setMessage(error instanceof Error ? error.message : "Send to STREAM failed.");
            })
            .finally(() => setBusy(false));
        }}
      >
        {SEND_TO_STREAM_LABEL}
      </button>
      {message ? <p className="section-copy">{message}</p> : null}
    </div>
  );
}
