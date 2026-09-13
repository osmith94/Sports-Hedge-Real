"use client";

import { useEffect, useState } from "react";

import { LiveRefreshStatus, VenueHealth, getLiveRefreshStatus, getVenueHealth } from "../lib/api";
import { relativeTime } from "../lib/format";

const FIRST_CLASS: Array<{ venue: VenueHealth["venue"]; label: string }> = [
  { venue: "matchbook", label: "Matchbook" },
  { venue: "polymarket", label: "Polymarket" },
  { venue: "kalshi", label: "Kalshi" },
];

function tone(row: VenueHealth | undefined): "ok" | "warn" | "down" | "unknown" {
  if (!row) return "unknown";
  if (!row.ok) return "down";
  if (row.authenticated) return "ok";
  return "warn";
}

function caption(row: VenueHealth | undefined, label: string): string {
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
    Promise.all([
      getVenueHealth().catch(() => [] as VenueHealth[]),
      getLiveRefreshStatus().catch(() => null),
    ]).then(([health, status]) => {
      if (cancelled) return;
      setRows(health);
      setRefresh(status);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const byVenue = new Map((rows ?? []).map((row) => [row.venue, row]));

  return (
    <div className="status-cluster" aria-label="First-class venue data health">
      {FIRST_CLASS.map((item) => {
        const row = byVenue.get(item.venue);
        const kind = tone(row);
        return (
          <span className="status-item" key={item.venue} title={row?.detail ?? caption(row, item.label)}>
            <span className={`status-dot ${kind}`} />
            {caption(row, item.label)}
          </span>
        );
      })}
      <span className="status-item muted">
        {refresh?.last_completed_at
          ? `Last scan ${relativeTime(refresh.last_completed_at)}`
          : "Last scan never"}
        {refresh?.last_error ? ` · ${refresh.last_error}` : ""}
      </span>
    </div>
  );
}
