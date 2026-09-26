"use client";

import { useEffect, useState } from "react";

import {
  VenueHealth,
  getVenueDegradationIncident,
  getVenueHealth,
} from "../lib/api";
import { useHydratedNowMs } from "./hydrated-relative-time";
import { useLiveStatus } from "./live-status-provider";
import { dualScanStatusLines } from "../lib/scan-status-display";
import { downloadVenueWhyIncident } from "../lib/venue-degradation-incident";
import { scanHealthTone, venueHealthCaption, venueHealthNeedsWhy } from "../lib/venue-health-display";
import { SystemLoadSummaryCard } from "./system-load-summary";

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
  const { status: refresh } = useLiveStatus();
  const nowMs = useHydratedNowMs();

  useEffect(() => {
    let cancelled = false;
    void getVenueHealth()
      .then((health) => {
        if (!cancelled) setRows(health);
      })
      .catch(() => {
        if (!cancelled) setRows([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const byVenue = new Map((rows ?? []).map((row) => [row.venue, row]));
  const scanHealth = refresh?.venue_health ?? {};

  return (
    <div className="status-cluster" aria-label="First-class venue data health">
      <SystemLoadSummaryCard status={refresh} />
      <div className="status-side">
        <div className="status-venues">
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
                      void getVenueDegradationIncident(item.venue)
                        .then((incident) => downloadVenueWhyIncident(incident))
                        .catch(() => undefined);
                    }}
                  >
                    Why?
                  </button>
                ) : null}
              </span>
            );
          })}
          {refresh?.paper_autofill_enabled ? (
            <span className="status-item status-item-capture" aria-label="AUTO PAPER CAPTURE ON">
              AUTO PAPER CAPTURE ON
            </span>
          ) : null}
        </div>
        <details className="status-lanes-diagnostics">
          <summary>Lane diagnostics</summary>
          <div className="status-lanes" aria-label="Scanner lane status">
            {dualScanStatusLines(refresh, nowMs).map((line) => {
              const sep = line.indexOf(" · ");
              const key = sep >= 0 ? line.slice(0, sep) : line;
              const detail = sep >= 0 ? line.slice(sep + 3) : "";
              return (
                <div className="status-lane" key={line} title={line} aria-label={line}>
                  <span className="status-lane-key">{key}</span>
                  <span className="status-lane-detail">{detail}</span>
                </div>
              );
            })}
          </div>
        </details>
        {refresh?.last_error ? (
          <span className="status-item status-item-error">{refresh.last_error}</span>
        ) : null}
      </div>
    </div>
  );
}
