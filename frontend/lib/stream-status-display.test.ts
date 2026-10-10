import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

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

test("idle STREAM is off and labelled not selected", () => {
  assert.equal(isStreamOff(idle), true);
  assert.equal(streamStatusLabel(idle), "NOT SELECTED");
  assert.match(streamFixtureName(idle), /No fixture selected/);
});

test("candidate copy stays shadow-only", () => {
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
  assert.equal(isStreamOff(live), false);
  assert.equal(streamFixtureName(live), "Arsenal v Chelsea");
  assert.match(streamCandidateSummary(live), /not executable/);
  assert.match(STREAM_COPY, /not paper entry/i);
  assert.match(STREAM_SHADOW_LABEL, /NOT PRICE-2/);
});

test("frontend STREAM surfaces do not call Price-2 or paper OPEN", () => {
  const root = join(process.cwd(), "components");
  const panel = readFileSync(join(root, "stream-panel.tsx"), "utf8");
  const pin = readFileSync(join(root, "stream-pin-controls.tsx"), "utf8");
  assert.doesNotMatch(panel, /simulate-fill|places_orders|price2/);
  assert.doesNotMatch(pin, /simulate-fill|places_orders|price2/);
  assert.match(panel, /getStreamStatus/);
});
