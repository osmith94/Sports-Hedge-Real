import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, it } from "vitest";

import type { StreamStatus } from "../lib/api";
import { StreamPanelView } from "./stream-panel";

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
  execution_enabled: false,
  candidates: [],
  limitations: [],
};

function render(status: StreamStatus | null) {
  return renderToStaticMarkup(
    createElement(StreamPanelView, {
      status,
      error: null,
      busy: false,
      onPause: () => undefined,
      onResume: () => undefined,
      onRemove: () => undefined,
    }),
  );
}

describe("stream-panel compact card", () => {
  it("empty state has EXECUTION OFF, no market table, collapsed diagnostics", () => {
    const html = render(idle);
    assert.match(html, /EXECUTION OFF/);
    assert.match(html, /NOT SELECTED/);
    assert.match(html, /No fixture selected/);
    assert.doesNotMatch(html, /<table/);
    assert.match(html, /<details class="stream-diagnostics"/);
    assert.match(html, /Details \/ Diagnostics/);
    assert.match(html, /disabled/);
  });

  it("paused 0/19 books are labelled not current", () => {
    const html = render({
      ...idle,
      paused: true,
      connection_status: "paused",
      canonical_event_id: "evt-1",
      home_team: "Real Madrid",
      away_team: "Villarreal",
      competition: "La Liga",
      token_id_count: 19,
      subscribed_market_count: 9,
      populated_pm_book_count: 0,
      books_current: false,
      candidates_current: false,
      metrics_scope: "session",
      price2_attempt_count: 0,
      price2_accepted_count: 0,
      valid_pair_count: 0,
      potential_arb_count: 0,
      paper_fill_count: 0,
      candidates: [
        {
          catalogue_row_id: "amc-1",
          register_canonical_key: "MATCH_RESULT_FT",
          trustworthy: false,
          rejection_reasons: ["unknown_required_venue_cost:polymarket", "missing_fx_rate:USD"],
          data_class: "stream_shadow_candidate",
          executable: false,
          price2: false,
          paper_entry: false,
          data_valid: false,
          economics_state: "blocked",
          top_reason: "unknown_required_venue_cost:polymarket",
          current: false,
        },
      ],
    });
    assert.match(html, /Real Madrid v Villarreal/);
    assert.match(html, /PAUSED/);
    assert.match(html, /0\/19/);
    assert.match(html, /not current/i);
    assert.match(html, /missing PM fee/);
    assert.match(html, /is-cached/);
    assert.doesNotMatch(html, /STREAM · Price-2 handoff · execution disarmed/);
  });

  it("valid no-arb row is not a Price-2 fill", () => {
    const html = render({
      ...idle,
      enabled: true,
      connection_status: "subscribed",
      canonical_event_id: "evt-1",
      home_team: "Arsenal",
      away_team: "Chelsea",
      token_id_count: 3,
      subscribed_market_count: 1,
      populated_pm_book_count: 3,
      coalesced_matchbook_quote_count: 1,
      books_current: true,
      candidates_current: true,
      valid_pair_count: 1,
      potential_arb_count: 0,
      candidates: [
        {
          catalogue_row_id: "amc-1",
          register_canonical_key: "MATCH_RESULT_FT",
          trustworthy: false,
          net_edge: "0.001",
          rejection_reasons: ["net_edge_below_threshold"],
          data_class: "stream_shadow_candidate",
          executable: false,
          price2: false,
          paper_entry: false,
          data_valid: true,
          economics_state: "no_arb",
          top_reason: "net_edge_below_threshold",
          current: true,
          pair_age_ms: 120,
          skew_ms: 40,
        },
      ],
    });
    assert.match(html, />valid</);
    assert.match(html, /no arb/);
    assert.match(html, />none</);
    assert.match(html, /below min net/);
    assert.doesNotMatch(html, />paper fill</);
  });

  it("Price-2 accepted without fill is not labelled as a trade", () => {
    const html = render({
      ...idle,
      enabled: true,
      connection_status: "subscribed",
      canonical_event_id: "evt-1",
      home_team: "Arsenal",
      away_team: "Chelsea",
      books_current: true,
      candidates_current: true,
      price2_attempt_count: 1,
      price2_accepted_count: 1,
      paper_fill_count: 0,
      paper_opened: false,
      orders_placed: false,
      candidates: [
        {
          catalogue_row_id: "amc-1",
          register_canonical_key: "MATCH_RESULT_FT",
          trustworthy: true,
          net_edge: "0.012",
          rejection_reasons: [],
          data_class: "stream_shadow_candidate",
          executable: false,
          price2: true,
          paper_entry: false,
          data_valid: true,
          economics_state: "qualified",
          current: true,
        },
      ],
    });
    assert.match(html, /accepted/);
    assert.doesNotMatch(html, />paper fill</);
    assert.match(html, /no persisted paper fill/i);
  });
});
