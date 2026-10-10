import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import type { StreamStatus } from "./api";
import {
  STREAM_COPY,
  STREAM_SHADOW_LABEL,
  isStreamOff,
  streamCandidateSummary,
  streamFixtureName,
  streamStatusLabel,
} from "./stream-status-display";

const idle: StreamStatus = {
  module: "STREAM",
  phase: "phase_1_shadow_only",
  enabled: false,
  paused: false,
  connection_status: "not_selected",
  registered_market_count: 0,
  subscribed_market_count: 0,
  token_id_count: 0,
  skipped_unavailable: 0,
  reconnect_count: 0,
  matchbook_request_count: 0,
  matchbook_rate_limited_count: 0,
  coalesced_event_count: 0,
  dropped_event_count: 0,
  suppressed_event_count: 0,
  error_bad_message_count: 0,
  unknown_token_count: 0,
  ignored_best_bid_ask_count: 0,
  resync_count: 0,
  token_cap: 24,
  max_fixtures: 1,
  diagnostic_reads_fetch_providers: false,
  paper_opened: false,
  orders_placed: false,
  candidates: [],
  limitations: [],
};

describe("stream-status-display", () => {
  it("idle STREAM is off and labelled not selected", () => {
    expect(isStreamOff(idle)).toBe(true);
    expect(streamStatusLabel(idle)).toBe("NOT SELECTED");
    expect(streamFixtureName(idle)).toMatch(/No fixture selected/);
  });

  it("candidate copy stays shadow-only", () => {
    const live: StreamStatus = {
      ...idle,
      enabled: true,
      connection_status: "subscribed",
      canonical_event_id: "evt-1",
      home_team: "Arsenal",
      away_team: "Chelsea",
      candidates: [
        {
          catalogue_row_id: "amc-1",
          register_canonical_key: "MATCH_RESULT_FT",
          trustworthy: true,
          net_edge: "0.004",
          rejection_reasons: ["stream_shadow_not_executable"],
          data_class: "stream_shadow_candidate",
          executable: false,
          price2: false,
          paper_entry: false,
        },
      ],
    };
    expect(isStreamOff(live)).toBe(false);
    expect(streamFixtureName(live)).toBe("Arsenal v Chelsea");
    expect(streamCandidateSummary(live)).toMatch(/not executable/);
    expect(STREAM_COPY).toMatch(/not paper entry/i);
    expect(STREAM_SHADOW_LABEL).toMatch(/NOT PRICE-2/);
  });

  it("frontend STREAM surfaces do not call Price-2 or paper OPEN", () => {
    const root = join(process.cwd(), "components");
    const panel = readFileSync(join(root, "stream-panel.tsx"), "utf8");
    const pin = readFileSync(join(root, "stream-pin-controls.tsx"), "utf8");
    expect(panel).not.toMatch(/simulate-fill|places_orders|price2/);
    expect(pin).not.toMatch(/simulate-fill|places_orders|price2/);
    expect(panel).toMatch(/getStreamStatus/);
  });
});
