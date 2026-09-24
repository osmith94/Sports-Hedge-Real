import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { LaneRefreshStatus, LiveRefreshStatus } from "./api";
import { discoveryStatusBadgeLabel } from "./discovered-fixture-display";
import { formatObservationAge } from "./observation-age";
import {
  lastScanVenueClause,
  lastScanVenuesLabel,
  laneLastScanHealth,
  laneVenueTruths,
  venueChipLabel,
  venueChipTitle,
} from "./venue-participation-display";
import { dualScanStatusLines, fastScanCopy, fullSweepCopy } from "./scan-status-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function status(
  overrides: Omit<Partial<LiveRefreshStatus>, "hot" | "universe"> & {
    hot?: Partial<LaneRefreshStatus>;
    universe?: Partial<LaneRefreshStatus>;
  } = {},
): LiveRefreshStatus {
  const { hot, universe, ...rest } = overrides;
  return {
    discovery_source: "matchbook",
    matching_venue: "polymarket",
    server_loop_enabled: true,
    interval_seconds: 30,
    cycle_in_progress: false,
    live_scores: "unavailable_unless_matchbook_payload_includes_scores",
    discovered_fixtures: [],
    hot: {
      cadence_seconds: 30,
      last_completed_at: "2026-09-16T12:00:00Z",
      last_duration_ms: 4100,
      next_due_at: "2026-09-16T12:00:30Z",
      fixture_count: 7,
      active_venues: ["matchbook", "polymarket", "kalshi"],
      pending_venues: ["matchbook", "polymarket", "kalshi"],
      ...hot,
    },
    universe: {
      cadence_seconds: 180,
      active_venues: ["matchbook", "polymarket", "kalshi"],
      pending_venues: ["matchbook", "polymarket", "kalshi"],
      fixture_count: 40,
      ...universe,
    },
    venue_participation: {
      hot: ["matchbook", "polymarket", "kalshi"],
      universe: ["matchbook", "polymarket", "kalshi"],
      source: "operator",
    },
    ...rest,
  };
}

describe("Wave N4 venue participation honesty", () => {
  it("does not treat configured-on Matchbook as last-scan data when unavailable", () => {
    const health = {
      matchbook: "unavailable",
      polymarket: "ok",
      kalshi: "ok",
    };
    const live = status({
      venue_health: health,
      hot: { venue_health: health },
    });
    const now = Date.parse("2026-09-16T12:00:57Z");
    const mb = laneVenueTruths(live, "hot").find((row) => row.venue === "matchbook");
    assert.equal(mb?.configured, true);
    assert.equal(mb?.participated, false);
    assert.equal(mb?.providerFailed, true);
    assert.equal(venueChipLabel(mb!), "MB ON · UNAVAILABLE");
    assert.match(venueChipTitle(mb!, "HOT pricing"), /did not receive MB data/);
    assert.match(fastScanCopy(live, now).detail, /last scan PM·K/);
    assert.match(fastScanCopy(live, now).detail, /MB unavailable/);
    assert.doesNotMatch(fastScanCopy(live, now).detail, /last scan MB/);
    assert.equal(
      discoveryStatusBadgeLabel(true, live),
      "LIVE PAPER · PM / K · MB UNAVAILABLE",
    );
    assert.doesNotMatch(discoveryStatusBadgeLabel(true, live), /^LIVE PAPER · MB \/ PM \/ K$/);
    assert.match(lastScanVenuesLabel(live, true), /polymarket · kalshi/);
    assert.match(lastScanVenuesLabel(live, true), /matchbook unavailable/);
    assert.doesNotMatch(lastScanVenuesLabel(live, true), /^matchbook · polymarket · kalshi$/);
  });

  it("recovers Matchbook participation after provider health returns ok", () => {
    const recoveredHealth = { matchbook: "ok", polymarket: "ok", kalshi: "ok" };
    const recovered = status({
      venue_health: recoveredHealth,
      hot: { venue_health: recoveredHealth },
    });
    const mb = laneVenueTruths(recovered, "hot").find((row) => row.venue === "matchbook");
    assert.equal(venueChipLabel(mb!), "MB ON");
    assert.equal(mb?.participated, true);
    assert.match(fastScanCopy(recovered).detail, /last scan MB·PM·K/);
    assert.doesNotMatch(fastScanCopy(recovered).detail, /MB unavailable/);
    assert.equal(discoveryStatusBadgeLabel(true, recovered), "LIVE PAPER · MB / PM / K");
  });

  it("labels a PM/K-only scan without implying Matchbook supplied data", () => {
    const pmk = status({
      hot: {
        cadence_seconds: 30,
        last_completed_at: "2026-09-16T12:00:00Z",
        last_duration_ms: 4100,
        next_due_at: "2026-09-16T12:00:30Z",
        fixture_count: 4,
        active_venues: ["polymarket", "kalshi"],
        pending_venues: ["polymarket", "kalshi"],
        venue_health: {
          matchbook: "disabled",
          polymarket: "ok",
          kalshi: "ok",
        },
      },
      venue_participation: {
        hot: ["polymarket", "kalshi"],
        universe: ["polymarket", "kalshi"],
        source: "operator",
      },
      venue_health: {
        matchbook: "disabled",
        polymarket: "ok",
        kalshi: "ok",
      },
    });
    const mb = laneVenueTruths(pmk, "hot").find((row) => row.venue === "matchbook");
    assert.equal(venueChipLabel(mb!), "MB OFF");
    assert.match(fastScanCopy(pmk).detail, /last scan PM·K/);
    assert.doesNotMatch(fastScanCopy(pmk).detail, /MB unavailable|last scan MB/);
    assert.equal(discoveryStatusBadgeLabel(true, pmk), "LIVE PAPER · PM / K");
    assert.equal(lastScanVenueClause(pmk, "hot"), "last scan PM·K");
  });

  it("names completed vs next-due clocks instead of mixing unlabeled ages", () => {
    const now = Date.parse("2026-09-16T12:00:57Z");
    const live = status({
      venue_health: { matchbook: "ok", polymarket: "ok", kalshi: "ok" },
      hot: { venue_health: { matchbook: "ok", polymarket: "ok", kalshi: "ok" } },
    });
    const detail = fastScanCopy(live, now).detail;
    assert.match(detail, /completed 57s ago/);
    assert.match(detail, /next scan in 0s/);
    assert.match(detail, /ran 4.1s/);
    assert.doesNotMatch(detail, /^57s ago ·/);
    const stamped = dualScanStatusLines(live, null).join(" ");
    assert.match(stamped, /completed at 2026-09-16T12:00:00Z/);
    assert.match(stamped, /next scan 2026-09-16T12:00:30Z/);
    assert.doesNotMatch(stamped, /57s ago/);
  });

  it("labels an in-progress scan without a fake just-completed age", () => {
    const live = status({
      hot: {
        cadence_seconds: 30,
        cycle_in_progress: true,
        last_completed_at: "2026-09-16T12:00:00Z",
        last_duration_ms: 4100,
        next_due_at: "2026-09-16T12:00:30Z",
        fixture_count: 7,
        active_venues: ["matchbook", "polymarket", "kalshi"],
        pending_venues: ["matchbook", "polymarket", "kalshi"],
        venue_health: { matchbook: "unavailable", polymarket: "ok", kalshi: "ok" },
      },
      venue_health: { matchbook: "unavailable", polymarket: "ok", kalshi: "ok" },
    });
    assert.match(fastScanCopy(live, Date.parse("2026-09-16T12:00:57Z")).detail, /in progress/);
    assert.match(fastScanCopy(live).detail, /last scan PM·K/);
    assert.doesNotMatch(fastScanCopy(live).detail, /completed 0s ago|just now/);
  });

  it("keeps HOT pricing last-scan truth after a later UNIVERSE discovery recovery", () => {
    const hotFailed = { matchbook: "unavailable", polymarket: "ok", kalshi: "ok" };
    const universeOk = { matchbook: "ok", polymarket: "ok", kalshi: "ok" };
    const live = status({
      venue_health: universeOk,
      hot: { venue_health: hotFailed },
      universe: { venue_health: universeOk },
    });
    assert.deepEqual(laneLastScanHealth(live, "hot"), hotFailed);
    assert.deepEqual(laneLastScanHealth(live, "universe"), universeOk);
    const hotMb = laneVenueTruths(live, "hot").find((row) => row.venue === "matchbook");
    const universeMb = laneVenueTruths(live, "universe").find((row) => row.venue === "matchbook");
    assert.equal(venueChipLabel(hotMb!), "MB ON · UNAVAILABLE");
    assert.equal(venueChipLabel(universeMb!), "MB ON");
    assert.equal(lastScanVenueClause(live, "hot"), "last scan PM·K · MB unavailable");
    assert.equal(lastScanVenueClause(live, "universe"), "last scan MB·PM·K");
    assert.match(fastScanCopy(live).detail, /last scan PM·K/);
    assert.match(fastScanCopy(live).detail, /MB unavailable/);
    assert.doesNotMatch(fastScanCopy(live).detail, /last scan MB·PM·K/);
    assert.match(fullSweepCopy(live).detail, /last scan MB·PM·K/);
    assert.doesNotMatch(fullSweepCopy(live).detail, /MB unavailable/);
    assert.equal(discoveryStatusBadgeLabel(true, live), "LIVE PAPER · MB / PM / K");
  });

  it("keeps UNIVERSE discovery last-scan truth after a later HOT pricing recovery", () => {
    const universeFailed = { matchbook: "unavailable", polymarket: "ok", kalshi: "ok" };
    const hotOk = { matchbook: "ok", polymarket: "ok", kalshi: "ok" };
    const live = status({
      venue_health: hotOk,
      hot: { venue_health: hotOk },
      universe: { venue_health: universeFailed },
    });
    assert.equal(lastScanVenueClause(live, "hot"), "last scan MB·PM·K");
    assert.equal(lastScanVenueClause(live, "universe"), "last scan PM·K · MB unavailable");
    assert.match(fastScanCopy(live).detail, /last scan MB·PM·K/);
    assert.doesNotMatch(fastScanCopy(live).detail, /MB unavailable/);
    assert.match(fullSweepCopy(live).detail, /last scan PM·K/);
    assert.match(fullSweepCopy(live).detail, /MB unavailable/);
  });

  it("does not borrow global latest health for a lane with no last-scan snapshot", () => {
    const live = status({
      venue_health: { matchbook: "ok", polymarket: "ok", kalshi: "ok" },
    });
    assert.equal(laneLastScanHealth(live, "hot"), undefined);
    assert.match(lastScanVenueClause(live, "hot") ?? "", /availability not in this snapshot/);
    assert.doesNotMatch(fastScanCopy(live).detail, /last scan MB·PM·K/);
  });

  it("renders absolute timestamps until a client clock is supplied", () => {
    assert.equal(formatObservationAge("2026-09-16T12:00:00Z", null), "2026-09-16T12:00:00Z");
    assert.equal(formatObservationAge("2026-09-16T12:00:00Z", Date.parse("2026-09-16T12:00:08Z")), "8s");
    assert.equal(formatObservationAge("2026-09-16T12:00:00Z", Date.parse("2026-09-16T12:00:09Z")), "9s");
  });
});

describe("Wave N4 operator surface contracts", () => {
  it("derives chip and discovery badge labels from backend health, not a hardcoded MB/PM/K string", () => {
    const chips = readFileSync(join(frontendRoot, "components/venue-lane-controls.tsx"), "utf8");
    const discovery = readFileSync(join(frontendRoot, "lib/discovered-fixture-display.ts"), "utf8");
    const section = readFileSync(join(frontendRoot, "components/fixture-discovery-section.tsx"), "utf8");
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    const bar = readFileSync(join(frontendRoot, "components/venue-health-bar.tsx"), "utf8");
    const monitor = readFileSync(join(frontendRoot, "components/opportunity-monitor.tsx"), "utf8");
    const history = readFileSync(join(frontendRoot, "components/paper-scan-history-table.tsx"), "utf8");
    assert.match(chips, /venueChipLabel/);
    assert.match(chips, /providerFailed/);
    assert.doesNotMatch(chips, /\$\{item\.short\} \{on \? "ON" : "OFF"\}/);
    assert.match(discovery, /discoveryParticipatedVenues/);
    assert.doesNotMatch(discovery, /LIVE PAPER · MB \/ PM \/ K/);
    assert.match(section, /discoveryStatusBadgeLabel\(available, status\)/);
    assert.match(scan, /dualScanStatusLines\(liveRefresh, nowMs\)/);
    assert.match(bar, /dualScanStatusLines\(refresh, nowMs\)/);
    assert.match(monitor, /useState<number \| null>\(null\)/);
    assert.match(history, /useState<number \| null>\(null\)/);
    assert.match(monitor, /Last scan venues/);
    const display = readFileSync(join(frontendRoot, "lib/venue-participation-display.ts"), "utf8");
    assert.match(display, /laneLastScanHealth/);
    assert.match(display, /laneStatus\?\.venue_health/);
  });
});
