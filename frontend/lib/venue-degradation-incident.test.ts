import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  downloadVenueWhyIncident,
  venueDegradationFilename,
} from "./venue-degradation-incident";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

describe("degraded-venue Why? download layer", () => {
  it("builds a timestamped filename and downloads the backend JSON as-is", () => {
    const clicks: Array<{ href: string; download: string }> = [];
    const result = downloadVenueWhyIncident(
      {
        affected_venue: "matchbook",
        captured_at: "2026-09-19T20:05:06Z",
        incident_id: "inc-1",
        data_kind: "in_memory_transition_snapshot",
        classification: { hot_ok: true, universe_discovery_timeout: true },
      },
      {
        createObjectURL: () => "blob:why-incident",
        revokeObjectURL: () => undefined,
        click: (anchor) => clicks.push(anchor),
      },
    );
    assert.equal(result.filename, "sports-hedge-matchbook-degradation-2026-09-19T20-05-06Z.json");
    assert.equal(clicks[0]?.download, result.filename);
    assert.match(result.json, /"incident_id": "inc-1"/);
    assert.match(result.json, /"universe_discovery_timeout": true/);
    assert.equal(
      venueDegradationFilename("kalshi", "2026-09-19T21:00:00.000Z"),
      "sports-hedge-kalshi-degradation-2026-09-19T21-00-00-000Z.json",
    );
  });

  it("keeps frontend thin: Why? fetches the backend snapshot, not a local store", () => {
    const helper = readFileSync(join(frontendRoot, "lib/venue-degradation-incident.ts"), "utf8");
    const bar = readFileSync(join(frontendRoot, "components/venue-health-bar.tsx"), "utf8");
    const api = readFileSync(join(frontendRoot, "lib/api.ts"), "utf8");
    assert.doesNotMatch(helper, /classifyVenueDegradation|createVenueDegradationIncidentStore|observeVenueDegradationIncidents/);
    assert.doesNotMatch(bar, /createVenueDegradationIncidentStore|observeVenueDegradationIncidents/);
    assert.match(bar, /getVenueDegradationIncident\(item\.venue\)/);
    assert.match(bar, /downloadVenueWhyIncident\(incident\)/);
    assert.match(api, /\/paper\/venue-degradation-incident\//);
    const whyBlock = bar.slice(bar.indexOf("status-why"));
    assert.doesNotMatch(whyBlock, /getVenueHealth|getLiveRefreshStatus|\/venues\/health|collect_and_scan/);
  });
});
