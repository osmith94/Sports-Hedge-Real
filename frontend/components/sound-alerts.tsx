"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { getPrice2ActivityAttempts, getWatchlistActivity } from "../lib/api";
import {
  SOUND_LOOKBACK_MS,
  SOUND_POLL_MS,
  SOUND_QUEUE_LIMIT,
  SoundEventTracker,
  audibleEvents,
  type SoundCue,
} from "../lib/sound-alerts";

/**
 * Optional browser-only operator soundboard.
 * No provider requests, backend writes, order submissions or scanner changes.
 * Browser audio requires a user gesture, so sounds start OFF until clicked.
 */
export function SoundAlerts() {
  const [enabled, setEnabled] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const playing = useRef<HTMLAudioElement | null>(null);
  const queue = useRef<SoundCue[]>([]);
  const tracker = useRef<SoundEventTracker | null>(null);

  const playNext = useCallback(function next() {
    if (playing.current || !queue.current.length) return;
    const cue = queue.current.shift();
    if (!cue) return;
    const audio = new Audio(`/api/sounds/${cue}`);
    audio.volume = 0.7;
    playing.current = audio;
    let released = false;
    const release = () => {
      if (released) return;
      released = true;
      if (playing.current === audio) playing.current = null;
      next();
    };
    audio.addEventListener("ended", release, { once: true });
    audio.addEventListener("error", () => {
      setNotice("MP3 unavailable — check the local sounds folder.");
      release();
    }, { once: true });
    void audio.play().catch(() => {
      setNotice("Browser blocked audio — click Sounds OFF, then ON again.");
      release();
    });
  }, []);

  const enqueue = useCallback((cue: SoundCue) => {
    // Never stack identical loud announcements.
    if (queue.current.includes(cue)) return;
    if (queue.current.length >= SOUND_QUEUE_LIMIT) return;
    queue.current.push(cue);
    playNext();
  }, [playNext]);

  useEffect(() => {
    if (!enabled || !tracker.current) return;
    const session = tracker.current;
    let stopped = false;
    let busy = false;
    async function poll() {
      if (busy || stopped) return;
      busy = true;
      try {
        const since = encodeURIComponent(new Date(Date.now() - SOUND_LOOKBACK_MS).toISOString());
        // These are existing, read-only audit projections; never call a venue.
        const [lifecycle, price2] = await Promise.allSettled([
          getWatchlistActivity(`limit=100&operator_signal=true&since=${since}`),
          getPrice2ActivityAttempts(`limit=100&include_recent=true&since=${since}`),
        ]);
        if (stopped) return;
        const events = audibleEvents(
          lifecycle.status === "fulfilled" ? lifecycle.value : [],
          price2.status === "fulfilled" ? price2.value : [],
        );
        for (const cue of session.consume(events)) enqueue(cue);
      } finally {
        busy = false;
      }
    }
    void poll().catch(() => { /* An alert failure must never affect scanning. */ });
    const timer = window.setInterval(() => {
      void poll().catch(() => { /* Retry read-only on the next poll. */ });
    }, SOUND_POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [enabled, enqueue]);

  function toggle() {
    if (enabled) {
      setEnabled(false);
      tracker.current = null;
      queue.current = [];
      playing.current?.pause();
      playing.current = null;
      setNotice(null);
      return;
    }

    setNotice(null);
    tracker.current = new SoundEventTracker(Date.now());
    setEnabled(true);
    // Immediate sample in the click handler also unlocks browser audio policy.
    enqueue("arb_detected");
  }

  return (
    <div className="sound-alert-control">
      <button
        type="button"
        className={enabled ? "sound-alert-toggle is-on" : "sound-alert-toggle"}
        onClick={toggle}
        aria-pressed={enabled}
        aria-label={enabled ? "Turn GTA sound alerts off" : "Turn GTA sound alerts on"}
        title="Read-only sound notifications. Browser click required to enable. No trading impact."
      >
        {enabled ? "🔊 GTA sounds ON" : "🔇 GTA sounds OFF"}
      </button>
      {notice ? <span className="sound-alert-notice" role="status">{notice}</span> : null}
    </div>
  );
}
