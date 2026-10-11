import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import type { StreamStatus } from "./api";
import {
  STREAM_SHADOW_LABEL,
  formatStreamDuration,
  isStreamOff,
  streamCandidateSummary,
  streamFixtureName,
  streamRibbon,
  streamStatusLabel,
  streamTopReason,
} from "./stream-status-display";

const idle: StreamStatus = {
  module: "STREAM",
  phase: "wave38_stream_price2",
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

  it("candidate copy distinguishes telemetry from Price-2", () => {
    const live: StreamStatus = {
      ...idle,
      enabled: true,
      connection_status: "subscribed",
      canonical_event_id: "evt-1",
      home_team: "Arsenal",
      away_team: "Chelsea",
      candidates_current: true,
      books_current: true,
      candidates: [
        {
          catalogue_row_id: "amc-1",
          register_canonical_key: "MATCH_RESULT_FT",
          trustworthy: true,
          net_edge: "0.004",
          rejection_reasons: [],
          data_class: "stream_shadow_candidate",
          executable: false,
          price2: false,
          paper_entry: false,
          data_valid: true,
          economics_state: "qualified",
          current: true,
        },
      ],
    };
    expect(isStreamOff(live)).toBe(false);
    expect(streamFixtureName(live)).toBe("Arsenal v Chelsea");
    expect(streamCandidateSummary(live)).toMatch(/Price-2/i);
    expect(STREAM_SHADOW_LABEL).toBe("EXECUTION OFF");
  });

  it("paused cached rows are not current", () => {
    const paused: StreamStatus = {
      ...idle,
      paused: true,
      connection_status: "paused",
      canonical_event_id: "evt-1",
      home_team: "Real Madrid",
      away_team: "Villarreal",
      candidates_current: false,
      books_current: false,
      populated_pm_book_count: 0,
      token_id_count: 19,
      candidates: [
        {
          catalogue_row_id: "amc-1",
          register_canonical_key: "MATCH_RESULT_FT",
          trustworthy: false,
          rejection_reasons: ["missing_fx_rate:USD"],
          data_class: "stream_shadow_candidate",
          executable: false,
          price2: false,
          paper_entry: false,
          current: false,
          data_valid: false,
          economics_state: "blocked",
          top_reason: "missing_fx_rate:USD",
        },
      ],
    };
    expect(streamCandidateSummary(paused)).toMatch(/cached/i);
    expect(streamRibbon(paused)[0].value).toBe("0/19");
    expect(streamRibbon(paused)[0].tone).toBe("muted");
    expect(streamTopReason(paused.candidates[0])).toMatch(/FX unavailable/i);
  });

  it("formats pair age in human units", () => {
    expect(formatStreamDuration(250)).toBe("250 ms");
    expect(formatStreamDuration(1500)).toBe("1.5 s");
  });

  it("frontend STREAM surfaces do not call Price-2 or paper OPEN", () => {
    const root = join(process.cwd(), "components");
    const panel = readFileSync(join(root, "stream-panel.tsx"), "utf8");
    const pin = readFileSync(join(root, "stream-pin-controls.tsx"), "utf8");
    expect(panel).not.toMatch(/simulate-fill|places_orders/);
    expect(pin).not.toMatch(/simulate-fill|places_orders/);
    expect(panel).toMatch(/getStreamStatus/);
  });
});
