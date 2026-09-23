"use client";

import { LiveRefreshStatus, LaneVenueFlags, Venue, saveVenueParticipation } from "../lib/api";
import {
  OPERATOR_SCAN_VENUES,
  laneVenueTruths,
  venueChipLabel,
  venueChipTitle,
} from "../lib/venue-participation-display";

type OperatorVenue = keyof LaneVenueFlags;

const VENUES: Array<{ venue: OperatorVenue; short: string; label: string }> = [
  { venue: "matchbook", short: "MB", label: "Matchbook" },
  { venue: "kalshi", short: "K", label: "Kalshi" },
  { venue: "polymarket", short: "PM", label: "Polymarket" },
];

function flagsFromVenues(venues: Venue[] | undefined): LaneVenueFlags {
  const set = new Set(venues ?? OPERATOR_SCAN_VENUES);
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
        label="HOT pricing"
        lane="hot"
        flags={hotFlags}
        status={status}
        warning={status?.hot?.venue_warning}
        appliesNext={Boolean(status?.hot?.applies_next_cycle)}
        disabled={disabled}
        onToggle={(venue) => void toggle("hot", venue)}
      />
      <LaneRow
        label="UNIVERSE discovery"
        lane="universe"
        flags={universeFlags}
        status={status}
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
  lane,
  flags,
  status,
  warning,
  appliesNext,
  disabled,
  onToggle,
}: {
  label: string;
  lane: "hot" | "universe";
  flags: LaneVenueFlags;
  status: LiveRefreshStatus | null;
  warning?: string | null;
  appliesNext: boolean;
  disabled?: boolean;
  onToggle: (venue: OperatorVenue) => void;
}) {
  const count = enabledCount(flags);
  const truths = laneVenueTruths(status, lane);
  return (
    <div className="venue-lane-row">
      <div className="venue-lane-label">{label}</div>
      <div className="venue-lane-chips" role="group" aria-label={`${label} venues`}>
        {VENUES.map((item) => {
          const on = flags[item.venue];
          const truth = truths.find((row) => row.venue === item.venue) ?? {
            venue: item.venue,
            short: item.short,
            configured: on,
            participated: false,
            providerFailed: false,
            operatorDisabled: !on,
            health: undefined,
          };
          const chipLabel = venueChipLabel({ ...truth, configured: on });
          const unavailable = on && truth.providerFailed;
          return (
            <button
              key={item.venue}
              type="button"
              className={
                unavailable
                  ? "venue-lane-chip on unavail"
                  : on
                    ? "venue-lane-chip on"
                    : "venue-lane-chip off"
              }
              aria-pressed={on}
              aria-label={`${label} ${item.label} ${on ? "configured on" : "configured off"}${
                unavailable ? `, last scan ${truth.health}` : ""
              }`}
              title={venueChipTitle({ ...truth, configured: on }, label)}
              disabled={disabled}
              onClick={() => onToggle(item.venue)}
            >
              {chipLabel}
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
