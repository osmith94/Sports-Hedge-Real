from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StreamCandidateStatus(BaseModel):
    catalogue_row_id: str
    register_canonical_key: str
    trustworthy: bool
    net_edge: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    stream_quote_at: str | None = None
    matchbook_quote_at: str | None = None
    pair_age_ms: int | None = None
    skew_ms: int | None = None
    data_class: str = "stream_shadow_candidate"
    executable: bool = False
    price2: bool = False
    paper_entry: bool = False


class StreamStatus(BaseModel):
    module: str = "STREAM"
    phase: str = "phase_1_shadow_only"
    enabled: bool
    paused: bool
    connection_status: str
    canonical_event_id: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    competition: str | None = None
    registered_market_count: int = 0
    subscribed_market_count: int = 0
    token_id_count: int = 0
    skipped_unavailable: int = 0
    reconnect_count: int = 0
    last_full_snapshot_at: str | None = None
    last_incremental_update_at: str | None = None
    matchbook_request_count: int = 0
    matchbook_rate_limited_count: int = 0
    coalesced_event_count: int = 0
    dropped_event_count: int = 0
    suppressed_event_count: int = 0
    last_trigger_reason: str | None = None
    last_probability_delta: str | None = None
    last_dispatch_delay_ms: int | None = None
    matchbook_requests_by_trigger: dict[str, int] = Field(default_factory=dict)
    price_move_probability_points: str = "0.02"
    error_bad_message_count: int = 0
    unknown_token_count: int = 0
    ignored_best_bid_ask_count: int = 0
    resync_count: int = 0
    last_error: str | None = None
    token_cap: int
    max_fixtures: int
    diagnostic_reads_fetch_providers: bool = False
    paper_opened: bool = False
    orders_placed: bool = False
    candidates: list[StreamCandidateStatus] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    def as_public_dict(self) -> dict[str, Any]:
        return self.model_dump()
