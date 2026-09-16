import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  isOperatorDisabledHealth,
  isProviderHealthFailure,
  pulseVenueTone,
  scanHealthTone,
  venueHealthCaption,
  venueHealthIsDegraded,
} from "./venue-health-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

describe("operator-disabled venue health vs provider failure", () => {
  it("treats disabled as configuration/state, not an outage", () => {
    assert.equal(isOperatorDisabledHealth("disabled"), true);
    assert.equal(isProviderHealthFailure("disabled"), false);
    assert.equal(scanHealthTone("disabled"), "off");
    assert.equal(pulseVenueTone("disabled"), "off");
    assert.equal(venueHealthCaption("Polymarket", "disabled"), "Polymarket off (operator)");
    assert.doesNotMatch(venueHealthCaption("Polymarket", "disabled"), /unavailable|timeout|outage/i);
  });

  it("keeps genuine provider failures distinct", () => {
    assert.equal(isProviderHealthFailure("unavailable"), true);
    assert.equal(isProviderHealthFailure("timeout"), true);
    assert.equal(isProviderHealthFailure("degraded"), true);
    assert.equal(scanHealthTone("unavailable"), "down");
    assert.equal(scanHealthTone("timeout"), "warn");
    assert.equal(venueHealthCaption("Kalshi", "unavailable"), "Kalshi unavailable");
    assert.equal(venueHealthCaption("Matchbook", "timeout"), "Matchbook timeout");
    assert.equal(
      venueHealthIsDegraded({ matchbook: "ok", polymarket: "disabled", kalshi: "timeout" }),
      true,
    );
  });

  it("does not treat healthy public Kalshi data as read-only merely because it is unauthenticated", () => {
    assert.equal(venueHealthCaption("Kalshi", "ok", { ok: true, authenticated: false }), "Kalshi live data");
    assert.equal(venueHealthCaption("Kalshi", undefined, { ok: true, authenticated: false }), "Kalshi live data");
    assert.equal(venueHealthCaption("Polymarket", "ok", { ok: true, authenticated: false }), "Polymarket data");
    assert.equal(venueHealthCaption("Matchbook", "ok", { ok: true, authenticated: true }), "Matchbook data");
    assert.doesNotMatch(venueHealthCaption("Kalshi", "ok", { ok: true, authenticated: false }), /read-only/i);
    assert.equal(venueHealthCaption("Kalshi", "unavailable"), "Kalshi unavailable");
  });

  it("does not mark a scan degraded merely because a venue is operator-disabled", () => {
    assert.equal(
      venueHealthIsDegraded({
        matchbook: "ok",
        polymarket: "disabled",
        kalshi: "ok",
      }),
      false,
    );
    assert.equal(
      venueHealthIsDegraded({
        matchbook: "disabled",
        polymarket: "disabled",
        kalshi: "disabled",
      }),
      false,
    );
  });

  it("health bar and pulse render disabled as off, not a fake provider failure", () => {
    const bar = readFileSync(join(frontendRoot, "components/venue-health-bar.tsx"), "utf8");
    const pulse = readFileSync(join(frontendRoot, "components/live-scan-pulse.tsx"), "utf8");
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    assert.match(bar, /venueHealthCaption/);
    assert.match(bar, /scanHealthTone/);
    assert.match(pulse, /pulseVenueTone/);
    assert.match(scan, /venueHealthIsDegraded/);
    assert.doesNotMatch(bar, /scan === "unavailable".*disabled/);
    assert.match(pulse, /Scan failed/);
    assert.match(pulse, /Partial venue failure/);
    assert.match(pulse, /Provider unhealthy/);
    assert.doesNotMatch(pulse, /phase === "degraded"[\s\S]*Scan failed/);
  });
});
