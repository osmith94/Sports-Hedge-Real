"use client";

import { LiveRefreshStatus, LaneVenueFlags, Venue, saveVenueParticipation } from "../lib/api";

type OperatorVenue = keyof LaneVenueFlags;

const VENUES: Array<{ venue: OperatorVenue; short: string; label: string }> = [
  { venue: "matchbook", short: "MB", label: "Matchbook" },
  { venue: "kalshi", short: "K", label: "Kalshi" },
  { venue: "polymarket", short: "PM", label: "Polymarket" },
];

function flagsFromVenues(venues: Venue[] | undefined): LaneVenueFlags {
  const set = new Set(venues ?? ["matchbook", "polymarket", "kalshi"]);
  return {
    matchbook: set.has("matchbook"),
    kalshi: set.has("kalshi"),
    polymarket: set.has("polymarket"),
  };
}

function enabledCount(flags: LaneVenueFlags): number {
  return Number(flags.matchbook) + Number(flags.kalshi) + Number(flags.polymarket);
}

export function VenueLaneControls({
  status,
  onUpdated,
  disabled,
}: {
  status: LiveRefreshStatus | null;
  onUpdated: (next: LiveRefreshStatus) => void;
  disabled?: boolean;
}) {
  const hotFlags = flagsFromVenues(status?.hot?.pending_venues ?? status?.venue_participation?.hot);
  const universeFlags = flagsFromVenues(
    status?.universe?.pending_venues ?? status?.venue_participation?.universe,
  );

  async function toggle(lane: "hot" | "universe", venue: OperatorVenue) {
    const current = lane === "hot" ? hotFlags : universeFlags;
    const nextFlags: LaneVenueFlags = { ...current, [venue]: !current[venue] };
    const update =
      lane === "hot"
        ? { hot: nextFlags, universe: universeFlags }
        : { hot: hotFlags, universe: nextFlags };
    try {
      onUpdated(await saveVenueParticipation(update));
    } catch {
      // Next live-refresh poll restores the backend-owned selection.
    }
  }

  return (
    <div className="venue-lane-controls" aria-label="Lane venue participation">
      {status?.venue_participation?.config_diagnostic ? (
        <p className="venue-lane-warning" role="status">
          {status.venue_participation.config_diagnostic}
        </p>
      ) : null}
      <LaneRow
        label="Fast scan"
        flags={hotFlags}
        warning={status?.hot?.venue_warning}
        appliesNext={Boolean(status?.hot?.applies_next_cycle)}
        disabled={disabled}
        onToggle={(venue) => void toggle("hot", venue)}
      />
      <LaneRow
        label="Full sweep"
        flags={universeFlags}
        warning={status?.universe?.venue_warning}
        appliesNext={Boolean(status?.universe?.applies_next_cycle)}
        disabled={disabled}
        onToggle={(venue) => void toggle("universe", venue)}
      />
    </div>
  );
}

function LaneRow({
  label,
  flags,
  warning,
  appliesNext,
  disabled,
  onToggle,
}: {
  label: string;
  flags: LaneVenueFlags;
  warning?: string | null;
  appliesNext: boolean;
  disabled?: boolean;
  onToggle: (venue: OperatorVenue) => void;
}) {
  const count = enabledCount(flags);
  return (
    <div className="venue-lane-row">
      <div className="venue-lane-label">{label}</div>
      <div className="venue-lane-chips" role="group" aria-label={`${label} venues`}>
        {VENUES.map((item) => {
          const on = flags[item.venue];
          return (
            <button
              key={item.venue}
              type="button"
              className={on ? "venue-lane-chip on" : "venue-lane-chip off"}
              aria-pressed={on}
              aria-label={`${label} ${item.label} ${on ? "on" : "off"}`}
              title={`${item.label} ${on ? "ON — provider calls on the next cycle" : "OFF — no discovery/market/book calls for this lane"}`}
              disabled={disabled}
              onClick={() => onToggle(item.venue)}
            >
              {item.short} {on ? "ON" : "OFF"}
            </button>
          );
        })}
      </div>
      {count < 2 ? (
        <p className="venue-lane-warning" role="status">
          Fewer than two venues enabled — arbitrage comparison is not executable for {label}
          (operator selection, not a provider outage).
        </p>
      ) : null}
      {appliesNext ? (
        <p className="venue-lane-pending" role="status">
          Change applies at the next {label} cycle boundary.
        </p>
      ) : null}
      {warning && count >= 2 ? (
        <p className="venue-lane-warning" role="status">
          {warning}
        </p>
      ) : null}
    </div>
  );
}
