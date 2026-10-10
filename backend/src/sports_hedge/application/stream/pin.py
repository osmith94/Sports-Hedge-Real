"""Manual one-fixture STREAM pin from the durable approved catalogue."""

from __future__ import annotations

from dataclasses import dataclass, field

from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    DerivedPriceEngineItem,
    derived_price_engine_working_set,
    executable_polymarket_token_ids,
    required_outcomes_for_key,
)
from sports_hedge.application.stream.protocol import MAX_STREAM_FIXTURES, MAX_STREAM_TOKENS
from sports_hedge.domain.market_scope import MarketScope


class StreamPinError(ValueError):
    def __init__(self, reason: str, *, detail: str | None = None) -> None:
        self.reason = reason
        super().__init__(detail or reason)


@dataclass(frozen=True)
class StreamMarketPin:
    identity: DerivedPriceEngineItem
    token_ids: tuple[str, ...]
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class StreamFixturePin:
    canonical_event_id: str
    home_canonical: str | None
    away_canonical: str | None
    competition: str | None
    markets: tuple[StreamMarketPin, ...]
    skipped_unavailable: int
    token_ids: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        tokens: list[str] = []
        seen: set[str] = set()
        for market in self.markets:
            if market.unavailable_reason:
                continue
            for token in market.token_ids:
                if token in seen:
                    continue
                seen.add(token)
                tokens.append(token)
        object.__setattr__(self, "token_ids", tuple(tokens))

    @property
    def subscribed_market_count(self) -> int:
        return sum(1 for market in self.markets if not market.unavailable_reason)


def streamable_markets(rows: list[ApprovedMarketCatalogueRow]) -> StreamFixturePin:
    if MAX_STREAM_FIXTURES != 1:
        raise StreamPinError("phase1_one_fixture_only")
    events = {row.canonical_event_id for row in rows if str(row.canonical_event_id or "").strip()}
    if not events:
        raise StreamPinError("no_active_catalogue_rows")
    if len(events) != 1:
        raise StreamPinError("fixture_identity_ambiguous")
    canonical_event_id = next(iter(events))
    working = derived_price_engine_working_set(
        [
            row
            for row in rows
            if row.row_state is CatalogueRowState.ACTIVE
            and row.market_scope is MarketScope.FIXTURE_MATCH
        ]
    )
    if not working:
        raise StreamPinError("no_active_catalogue_rows")
    markets: list[StreamMarketPin] = []
    skipped = 0
    for identity in working:
        reason = _unavailable_reason(identity)
        tokens = ()
        if reason is None:
            cleaned = executable_polymarket_token_ids(
                list(identity.polymarket_token_ids),
                event_id=identity.polymarket_event_id,
                market_id=identity.polymarket_market_id,
                condition_id=identity.polymarket_condition_id,
                required_outcomes=list(identity.required_outcomes)
                or required_outcomes_for_key(identity.register_canonical_key),
            )
            tokens = tuple(item.native_id for item in cleaned)
        else:
            skipped += 1
        markets.append(
            StreamMarketPin(identity=identity, token_ids=tokens, unavailable_reason=reason)
        )
    pin = StreamFixturePin(
        canonical_event_id=canonical_event_id,
        home_canonical=working[0].home_canonical,
        away_canonical=working[0].away_canonical,
        competition=working[0].competition,
        markets=tuple(markets),
        skipped_unavailable=skipped,
    )
    if not pin.token_ids:
        raise StreamPinError("missing_polymarket_native_ids")
    if len(pin.token_ids) > MAX_STREAM_TOKENS:
        raise StreamPinError("stream_token_cap_exceeded")
    return pin


def _unavailable_reason(identity: DerivedPriceEngineItem) -> str | None:
    if not identity.matchbook_event_id or not identity.matchbook_market_id:
        return "missing_matchbook_native_ids"
    if not identity.polymarket_event_id or not identity.polymarket_market_id:
        return "missing_polymarket_native_ids"
    tokens = executable_polymarket_token_ids(
        list(identity.polymarket_token_ids),
        event_id=identity.polymarket_event_id,
        market_id=identity.polymarket_market_id,
        condition_id=identity.polymarket_condition_id,
        required_outcomes=list(identity.required_outcomes)
        or required_outcomes_for_key(identity.register_canonical_key),
    )
    if not tokens:
        return "missing_polymarket_native_ids"
    return None
