"use client";

import { useEffect, useRef, useState } from "react";

import { LiveRefreshStatus, VenueHealth, getLiveRefreshStatus, getVenueHealth } from "../lib/api";
import { useHydratedNowMs } from "./hydrated-relative-time";
import { applyLatestLiveRefresh, createLiveRefreshPollGuard } from "../lib/live-refresh-poll-guard";
import { dualScanStatusLines } from "../lib/scan-status-display";
import {
  createVenueDegradationIncidentStore,
  downloadVenueWhyIncident,
  observeVenueDegradationIncidents,
  resolveVenueWhyIncident,
} from "../lib/venue-degradation-incident";
import { scanHealthTone, venueHealthCaption, venueHealthNeedsWhy } from "../lib/venue-health-display";

const FIRST_CLASS: Array<{ venue: VenueHealth["venue"]; label: string }> = [
  { venue: "matchbook", label: "Matchbook" },
  { venue: "polymarket", label: "Polymarket" },
  { venue: "kalshi", label: "Kalshi" },
];

function tone(
  row: VenueHealth | undefined,
  scan?: string,
): "ok" | "warn" | "down" | "unknown" | "off" {
  const fromScan = scanHealthTone(scan);
  if (fromScan) return fromScan;
  if (!row) return "unknown";
  if (!row.ok) return "down";
  return "ok";
}

export function VenueHealthBar() {
  const [rows, setRows] = useState<VenueHealth[] | null>(null);
  const [refresh, setRefresh] = useState<LiveRefreshStatus | null>(null);
  const nowMs = useHydratedNowMs();
  const liveRefreshPollGuardRef = useRef(createLiveRefreshPollGuard());
  const incidentStoreRef = useRef(createVenueDegradationIncidentStore());
  const [incidents, setIncidents] = useState(incidentStoreRef.current.latest);

  useEffect(() => {
    let cancelled = false;
    const pollLiveRefresh = () =>
      applyLatestLiveRefresh(liveRefreshPollGuardRef.current, getLiveRefreshStatus, (status) => {
        if (cancelled) return;
        setRefresh(status);
        setIncidents(observeVenueDegradationIncidents(incidentStoreRef.current, status));
      }).catch(() => undefined);
    void pollLiveRefresh();
    void getVenueHealth()
      .then((health) => {
        if (!cancelled) setRows(health);
      })
      .catch(() => {
        if (!cancelled) setRows([]);
      });
    const timer = window.setInterval(() => {
      void pollLiveRefresh();
    }, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  const byVenue = new Map((rows ?? []).map((row) => [row.venue, row]));
  const scanHealth = refresh?.venue_health ?? {};

  return (
    <div className="status-cluster" aria-label="First-class venue data health">
      {FIRST_CLASS.map((item) => {
        const row = byVenue.get(item.venue);
        const scan = scanHealth[item.venue];
        const kind = tone(row, scan);
        return (
          <span className="status-item" key={item.venue} title={row?.detail ?? venueHealthCaption(item.label, scan, row)}>
            <span className={`status-dot ${kind}`} />
            {venueHealthCaption(item.label, scan, row)}
            {venueHealthNeedsWhy(scan) ? (
              <button
                type="button"
                className="status-why"
                aria-label={`Why is ${item.label} degraded?`}
                onClick={() => {
                  const incident = resolveVenueWhyIncident(item.venue, incidents, refresh);
                  if (!incident) return;
                  downloadVenueWhyIncident(incident);
                }}
              >
                Why?
              </button>
            ) : null}
          </span>
        );
      })}
      {refresh?.paper_autofill_enabled ? (
        <span className="status-item" aria-label="AUTO PAPER CAPTURE ON">
          AUTO PAPER CAPTURE ON
        </span>
      ) : null}
      {dualScanStatusLines(refresh, nowMs).map((line) => (
        <span className="status-item muted" key={line} aria-label={line}>
          {line}
        </span>
      ))}
      {refresh?.last_error ? (
        <span className="status-item muted">{refresh.last_error}</span>
      ) : null}
    </div>
  );
}
