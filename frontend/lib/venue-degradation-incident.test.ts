import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import type { LiveRefreshStatus } from "./api";
import {
  MAX_VENUE_DEGRADATION_INCIDENTS,
  VENUE_DEGRADATION_FALLBACK_KIND,
  createVenueDegradationIncidentStore,
  downloadVenueWhyIncident,
  fallbackVenueDegradationIncident,
  observeVenueDegradationIncidents,
  resolveVenueWhyIncident,
  venueDegradationFilename,
} from "./venue-degradation-incident";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function status(overrides: Partial<LiveRefreshStatus> = {}): LiveRefreshStatus {
  return {
    discovery_source: "matchbook",
    server_loop_enabled: false,
    interval_seconds: 30,
    cycle_in_progress: false,
    live_scores: "unavailable_unless_matchbook_payload_includes_scores",
    discovered_fixtures: [],
    venue_health: { matchbook: "ok", polymarket: "ok", kalshi: "ok" },
    ...overrides,
  };
}

describe("degraded-venue Why? incident diagnostic", () => {
  it("captures once on OK→degraded and does not spam while still degraded", () => {
    const store = createVenueDegradationIncidentStore();
    const healthy = status();
    const degraded = status({
      venue_health: { matchbook: "degraded", polymarket: "ok", kalshi: "ok" },
      hot: { cadence_seconds: 30, venue_health: { matchbook: "ok" } },
      universe: {
        cadence_seconds: 8,
        venue_health: { matchbook: "discovery_timeout" },
        operation_health: { matchbook: { list_events: "discovery_timeout" } },
      },
    });
    assert.deepEqual(observeVenueDegradationIncidents(store, healthy, "2026-09-19T20:00:00Z"), {});
    const first = observeVenueDegradationIncidents(store, degraded, "2026-09-19T20:00:05Z");
    assert.equal(Object.keys(first).length, 1);
    assert.equal(first.matchbook.captured_at, "2026-09-19T20:00:05Z");
    const second = observeVenueDegradationIncidents(store, degraded, "2026-09-19T20:00:10Z");
    assert.equal(second.matchbook.captured_at, "2026-09-19T20:00:05Z");
    assert.equal(store.retained.length, 1);
  });

  it("captures a new incident after recovery then a second degradation", () => {
    const store = createVenueDegradationIncidentStore();
    const healthy = status();
    const degraded = status({
      venue_health: { matchbook: "degraded", polymarket: "ok", kalshi: "ok" },
    });
    observeVenueDegradationIncidents(store, healthy, "2026-09-19T20:01:00Z");
    const first = observeVenueDegradationIncidents(store, degraded, "2026-09-19T20:01:05Z");
    observeVenueDegradationIncidents(store, healthy, "2026-09-19T20:01:10Z");
    const second = observeVenueDegradationIncidents(store, degraded, "2026-09-19T20:01:15Z");
    assert.equal(first.matchbook.captured_at, "2026-09-19T20:01:05Z");
    assert.equal(second.matchbook.captured_at, "2026-09-19T20:01:15Z");
    assert.equal(store.retained.length, 2);
  });

  it("distinguishes HOT ok + UNIVERSE discovery timeout from HOT market timeout", () => {
    const mixed = status({
      venue_health: { matchbook: "degraded" },
      hot: {
        cadence_seconds: 30,
        venue_health: { matchbook: "ok" },
        operation_health: { matchbook: { order_book: "ok" } },
      },
      universe: {
        cadence_seconds: 8,
        venue_health: { matchbook: "discovery_timeout" },
        operation_health: { matchbook: { list_events: "discovery_timeout" } },
      },
    });
    const hotTimeout = status({
      venue_health: { matchbook: "market_timeout" },
      hot: {
        cadence_seconds: 30,
        venue_health: { matchbook: "market_timeout" },
        operation_health: { matchbook: { order_book: "market_timeout" } },
      },
      universe: {
        cadence_seconds: 8,
        venue_health: { matchbook: "ok" },
        operation_health: { matchbook: { list_events: "ok" } },
      },
    });
    const mixedIncident = fallbackVenueDegradationIncident(mixed, "matchbook", "2026-09-19T20:02:00Z");
    const hotIncident = fallbackVenueDegradationIncident(hotTimeout, "matchbook", "2026-09-19T20:02:01Z");
    assert.equal(mixedIncident.classification?.hot_ok, true);
    assert.equal(mixedIncident.classification?.universe_discovery_timeout, true);
    assert.equal(mixedIncident.classification?.hot_market_timeout, false);
    assert.equal(hotIncident.classification?.hot_ok, false);
    assert.equal(hotIncident.classification?.hot_market_timeout, true);
    assert.equal(hotIncident.classification?.universe_discovery_timeout, false);
  });

  it("keeps provider_capacity_saturated / waiting / deferred identifiable as local backpressure", () => {
    const payload = status({
      venue_health: { matchbook: "degraded" },
      hot: { cadence_seconds: 30, venue_health: { matchbook: "ok" } },
      universe: {
        cadence_seconds: 8,
        venue_health: { matchbook: "discovery_timeout" },
        operation_health: { matchbook: { list_events: "discovery_timeout" } },
      },
      background: {
        cadence_seconds: 180,
        venue_health: { matchbook: "deferred" },
        operation_health: { matchbook: { order_book: "provider_capacity_saturated" } },
      },
      price_engine: {
        background: {
          deferred: 4,
          provider_capacity_saturated: 4,
          retry_wait: 2,
        },
      },
      provider_access: {
        inflight: { matchbook: 4 },
        waiting: { matchbook: 3 },
        waiting_by_lane: { background: { matchbook: 3 } },
        limits: { matchbook: 4 },
      },
    });
    const incident = fallbackVenueDegradationIncident(payload, "matchbook");
    assert.equal(incident.classification?.local_backpressure, true);
    assert.equal(incident.classification?.universe_discovery_timeout, true);
    assert.equal(
      (incident.background as { operation_health?: Record<string, Record<string, string>> })
        ?.operation_health?.matchbook?.order_book,
      "provider_capacity_saturated",
    );
    assert.equal((incident.provider_access as { waiting?: Record<string, number> })?.waiting?.matchbook, 3);
  });

  it("bounds retained local incidents", () => {
    const store = createVenueDegradationIncidentStore();
    const healthy = status();
    const degraded = status({
      venue_health: { matchbook: "degraded", polymarket: "ok", kalshi: "ok" },
    });
    for (let index = 0; index < 12; index += 1) {
      observeVenueDegradationIncidents(store, healthy, `2026-09-19T20:03:${String(index).padStart(2, "0")}Z`);
      observeVenueDegradationIncidents(
        store,
        degraded,
        `2026-09-19T20:04:${String(index).padStart(2, "0")}Z`,
      );
    }
    assert.equal(store.retained.length, MAX_VENUE_DEGRADATION_INCIDENTS);
  });

  it("Why? downloads the captured JSON with a timestamped filename and zero extra fetches", () => {
    const captured = fallbackVenueDegradationIncident(
      status({ venue_health: { matchbook: "degraded" } }),
      "matchbook",
      "2026-09-19T20:05:06Z",
    );
    const clicks: Array<{ href: string; download: string }> = [];
    const result = downloadVenueWhyIncident(captured, {
      createObjectURL: () => "blob:why-incident",
      revokeObjectURL: () => undefined,
      click: (anchor) => clicks.push(anchor),
    });
    assert.equal(result.filename, "sports-hedge-matchbook-degradation-2026-09-19T20-05-06Z.json");
    assert.equal(clicks[0]?.download, result.filename);
    assert.match(result.json, /"affected_venue": "matchbook"/);
    assert.equal(venueDegradationFilename("kalshi", "2026-09-19T21:00:00.000Z"), "sports-hedge-kalshi-degradation-2026-09-19T21-00-00-000Z.json");
    const fallback = resolveVenueWhyIncident("matchbook", {}, status({ venue_health: { matchbook: "timeout" } }), "2026-09-19T20:06:00Z");
    assert.equal(fallback?.data_kind, VENUE_DEGRADATION_FALLBACK_KIND);
  });

  it("health bar Why? uses already-polled live-refresh and never calls venues/health", () => {
    const bar = readFileSync(join(frontendRoot, "components/venue-health-bar.tsx"), "utf8");
    const helper = readFileSync(join(frontendRoot, "lib/venue-degradation-incident.ts"), "utf8");
    assert.match(bar, /Why\?/);
    assert.match(bar, /downloadVenueWhyIncident\(incident\)/);
    assert.match(bar, /observeVenueDegradationIncidents/);
    assert.match(helper, /already_polled_live_refresh_read_model/);
    const whyBlock = bar.slice(bar.indexOf("status-why"));
    assert.doesNotMatch(whyBlock, /getVenueHealth|getLiveRefreshStatus|\/venues\/health|collect_and_scan/);
  });
});
