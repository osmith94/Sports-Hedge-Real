"use client";

import { useEffect, useState } from "react";

import { LiveRefreshStatus, VenueHealth, getLiveRefreshStatus, getVenueHealth } from "../lib/api";
import { dualScanStatusLines } from "../lib/scan-status-display";

const FIRST_CLASS: Array<{ venue: VenueHealth["venue"]; label: string }> = [
  { venue: "matchbook", label: "Matchbook" },
  { venue: "polymarket", label: "Polymarket" },
  { venue: "kalshi", label: "Kalshi" },
];

function scanTone(value: string | undefined): "ok" | "warn" | "down" | "unknown" | null {
  if (!value) return null;
  if (value === "ok") return "ok";
  if (value === "degraded" || value === "timeout") return "warn";
  if (value === "unavailable") return "down";
  return "unknown";
}

function tone(row: VenueHealth | undefined, scan?: string): "ok" | "warn" | "down" | "unknown" {
  const fromScan = scanTone(scan);
  if (fromScan) return fromScan;
  if (!row) return "unknown";
  if (!row.ok) return "down";
  if (row.authenticated) return "ok";
  return "warn";
}

function caption(row: VenueHealth | undefined, label: string, scan?: string): string {
  if (scan === "degraded") return `${label} degraded`;
  if (scan === "timeout") return `${label} timeout`;
  if (scan === "unavailable") return `${label} unavailable`;
  if (scan === "ok") return row?.authenticated ? `${label} data` : `${label} read-only`;
  if (!row) return `${label} health unknown`;
  if (!row.ok) return `${label} unavailable`;
  if (row.authenticated) return `${label} data`;
  return `${label} read-only`;
}

export function VenueHealthBar() {
  const [rows, setRows] = useState<VenueHealth[] | null>(null);
  const [refresh, setRefresh] = useState<LiveRefreshStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getLiveRefreshStatus()
      .then((status) => {
        if (!cancelled) setRefresh(status);
      })
      .catch(() => {
        if (!cancelled) setRefresh(null);
      });
    void getVenueHealth()
      .then((health) => {
        if (!cancelled) setRows(health);
      })
      .catch(() => {
        if (!cancelled) setRows([]);
      });
    const timer = window.setInterval(() => {
      void getLiveRefreshStatus()
        .then((status) => {
          if (!cancelled) setRefresh(status);
        })
        .catch(() => undefined);
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
          <span className="status-item" key={item.venue} title={row?.detail ?? caption(row, item.label, scan)}>
            <span className={`status-dot ${kind}`} />
            {caption(row, item.label, scan)}
          </span>
        );
      })}
      {dualScanStatusLines(refresh).map((line) => (
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
