from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sports_hedge.domain.football import FootballPeriod, MarketFamily, SettlementFingerprint, SettlementScope
from sports_hedge.domain.models import MarketSide
from sports_hedge.odds.ingestion import OddsIngestionService
from sports_hedge.odds.mapping import map_raw_record
from sports_hedge.odds.models import QuoteType, RawOddsRecord, VenueKind
from sports_hedge.odds.movement import (
    classify_open_close,
    open_close_pair_key,
    pair_open_close,
    revision_identity_key,
)
from sports_hedge.odds.repository import SqliteOddsRepository

RETRIEVED = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
KICKOFF = datetime(2025, 8, 16, 17, 30, tzinfo=UTC)


def _ah_record(*, quote_type: QuoteType, line: Decimal, odds: str, selection: str = "home") -> RawOddsRecord:
    return RawOddsRecord(
        source="synthetic",
        source_market_id=f"ah:{quote_type.value}:{line}:{selection}",
        source_reference=f"ah:{quote_type.value}:{line}:{selection}",
        competition="Premier League",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=KICKOFF,
        bookmaker="b365",
        venue="b365",
        market_family=MarketFamily.ASIAN_HANDICAP,
        period=FootballPeriod.FULL_TIME,
        line=line,
        selection=selection,
        decimal_odds=Decimal(odds),
        quote_type=quote_type,
        retrieved_at=RETRIEVED,
        settlement=SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            line=line,
            extra_time_included=False,
            penalties_included=False,
            push_possible=True,
        ),
        semantics_complete=True,
        raw_payload={"research_only": False},
    )


def test_same_line_asian_handicap_emits_price_logit_move() -> None:
    observations = [
        map_raw_record(
            _ah_record(quote_type=QuoteType.OPENING, line=Decimal("-0.5"), odds="1.90")
        ),
        map_raw_record(
            _ah_record(quote_type=QuoteType.CLOSING, line=Decimal("-0.5"), odds="2.05")
        ),
    ]
    classified = classify_open_close(observations)
    assert len(classified.price_moves) == 1
    assert classified.line_shifts == ()
    move = classified.price_moves[0]
    assert move.line == Decimal("-0.5")
    assert move.implied_logit_delta is not None
    assert move.implied_probability_delta != 0
    assert pair_open_close(observations) == list(classified.price_moves)


def test_changed_line_asian_handicap_is_line_shift_not_price_move() -> None:
    observations = [
        map_raw_record(
            _ah_record(quote_type=QuoteType.OPENING, line=Decimal("-0.5"), odds="1.90")
        ),
        map_raw_record(
            _ah_record(quote_type=QuoteType.CLOSING, line=Decimal("-0.75"), odds="1.95")
        ),
    ]
    classified = classify_open_close(observations)
    assert classified.price_moves == ()
    assert pair_open_close(observations) == []
    assert len(classified.line_shifts) == 1
    shift = classified.line_shifts[0]
    assert shift.opening_line == Decimal("-0.5")
    assert shift.closing_line == Decimal("-0.75")
    assert not hasattr(shift, "implied_probability_delta")
    assert not hasattr(shift, "implied_logit_delta")


def _record(**overrides: object) -> RawOddsRecord:
    settlement = overrides.pop("settlement", None)
    line = overrides.get("line", Decimal("-0.5"))
    period = overrides.get("period", FootballPeriod.FULL_TIME)
    if settlement is None:
        settlement = SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=period if isinstance(period, FootballPeriod) else FootballPeriod.FULL_TIME,
            line=line if isinstance(line, Decimal) else Decimal("-0.5"),
            extra_time_included=False,
            penalties_included=False,
            push_possible=True,
        )
    payload = {
        "source": "synthetic",
        "source_market_id": "ah-base",
        "source_reference": "ah-base",
        "competition": "Premier League",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "kickoff_utc": KICKOFF,
        "bookmaker": "b365",
        "venue": "b365",
        "venue_kind": VenueKind.BOOKMAKER,
        "market_family": MarketFamily.ASIAN_HANDICAP,
        "period": FootballPeriod.FULL_TIME,
        "line": Decimal("-0.5"),
        "selection": "home",
        "decimal_odds": Decimal("1.90"),
        "quote_type": QuoteType.OPENING,
        "retrieved_at": RETRIEVED,
        "settlement": settlement,
        "semantics_complete": True,
        "raw_payload": {"research_only": False},
    }
    payload.update(overrides)
    return RawOddsRecord(**payload)


def test_mixed_period_source_bookmaker_side_and_settlement_do_not_pair() -> None:
    opening = map_raw_record(_record(quote_type=QuoteType.OPENING, decimal_odds=Decimal("1.90")))
    mixed_period = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-fh",
            period=FootballPeriod.FIRST_HALF,
            decimal_odds=Decimal("2.00"),
            settlement=SettlementFingerprint(
                scope=SettlementScope.REGULATION_TIME,
                period=FootballPeriod.FIRST_HALF,
                line=Decimal("-0.5"),
                extra_time_included=False,
                penalties_included=False,
                push_possible=True,
            ),
        )
    )
    incomplete = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-incomplete",
            decimal_odds=Decimal("2.00"),
            semantics_complete=False,
            settlement=SettlementFingerprint(period=FootballPeriod.FULL_TIME, line=Decimal("-0.5")),
        )
    )
    other_settlement = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-et",
            decimal_odds=Decimal("2.00"),
            settlement=SettlementFingerprint(
                scope=SettlementScope.INCLUDING_EXTRA_TIME,
                period=FootballPeriod.FULL_TIME,
                line=Decimal("-0.5"),
                extra_time_included=True,
                penalties_included=False,
                push_possible=True,
            ),
        )
    )
    other_book = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-ps",
            bookmaker="ps",
            venue="ps",
            decimal_odds=Decimal("2.00"),
        )
    )
    other_source = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source="football_data",
            source_market_id="ah-fd",
            decimal_odds=Decimal("2.00"),
        )
    )
    other_side = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-lay",
            side=MarketSide.LAY,
            decimal_odds=Decimal("2.00"),
        )
    )
    classified = classify_open_close(
        [opening, mixed_period, incomplete, other_settlement, other_book, other_source, other_side]
    )
    assert classified.price_moves == ()
    assert classified.line_shifts == ()
    assert incomplete.market_equivalence_key() is None


def test_correction_uses_later_retrieved_at_regardless_of_input_order() -> None:
    opening = map_raw_record(_record(quote_type=QuoteType.OPENING, decimal_odds=Decimal("1.90")))
    original_close = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-close",
            decimal_odds=Decimal("2.00"),
            retrieved_at=RETRIEVED,
            raw_payload={"v": 1},
        )
    )
    corrected_close = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-close",
            decimal_odds=Decimal("2.20"),
            retrieved_at=RETRIEVED + timedelta(minutes=5),
            raw_payload={"v": 2},
        )
    )
    assert original_close.source_observation_key == corrected_close.source_observation_key
    forward = classify_open_close([opening, original_close, corrected_close])
    backward = classify_open_close([corrected_close, opening, original_close])
    assert len(forward.price_moves) == len(backward.price_moves) == 1
    assert forward.price_moves[0].closing_odds == Decimal("2.20")
    assert backward.price_moves[0].closing_odds == Decimal("2.20")


def test_line_correction_selects_current_revision_before_proposition() -> None:
    opening = map_raw_record(
        _record(
            quote_type=QuoteType.OPENING,
            source_market_id="ah-open",
            source_reference="ah-open",
            decimal_odds=Decimal("1.90"),
        )
    )
    stale_close = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-close",
            source_reference="ah-close",
            line=Decimal("-0.5"),
            decimal_odds=Decimal("2.00"),
            retrieved_at=RETRIEVED,
            raw_payload={"v": 1},
        )
    )
    corrected_close = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-close",
            source_reference="ah-close",
            line=Decimal("-0.75"),
            decimal_odds=Decimal("1.95"),
            retrieved_at=RETRIEVED + timedelta(minutes=5),
            raw_payload={"v": 2},
            settlement=SettlementFingerprint(
                scope=SettlementScope.REGULATION_TIME,
                period=FootballPeriod.FULL_TIME,
                line=Decimal("-0.75"),
                extra_time_included=False,
                penalties_included=False,
                push_possible=True,
            ),
        )
    )
    concurrent_open = map_raw_record(
        _record(
            quote_type=QuoteType.OPENING,
            source_market_id="ah-ps-open",
            source_reference="ah-ps-open",
            bookmaker="ps",
            venue="ps",
            decimal_odds=Decimal("1.88"),
        )
    )
    concurrent_close = map_raw_record(
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-ps-close",
            source_reference="ah-ps-close",
            bookmaker="ps",
            venue="ps",
            decimal_odds=Decimal("2.02"),
        )
    )
    assert stale_close.source_market_id == corrected_close.source_market_id == "ah-close"
    assert stale_close.source_reference == corrected_close.source_reference == "ah-close"
    assert revision_identity_key(stale_close) == revision_identity_key(corrected_close)
    assert revision_identity_key(concurrent_close) != revision_identity_key(corrected_close)

    rows = [opening, stale_close, corrected_close, concurrent_open, concurrent_close]
    forward = classify_open_close(rows)
    backward = classify_open_close(list(reversed(rows)))
    assert len(forward.price_moves) == len(backward.price_moves) == 1
    assert forward.price_moves[0].bookmaker == "ps"
    assert forward.price_moves[0].line == Decimal("-0.5")
    assert len(forward.line_shifts) == len(backward.line_shifts) == 1
    assert forward.line_shifts[0].bookmaker == "b365"
    assert forward.line_shifts[0].opening_line == Decimal("-0.5")
    assert forward.line_shifts[0].closing_line == Decimal("-0.75")

    records = [
        _record(
            quote_type=QuoteType.OPENING,
            source_market_id="ah-open",
            source_reference="ah-open",
            decimal_odds=Decimal("1.90"),
        ),
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-close",
            source_reference="ah-close",
            line=Decimal("-0.5"),
            decimal_odds=Decimal("2.00"),
            retrieved_at=RETRIEVED,
            raw_payload={"v": 1},
        ),
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-close",
            source_reference="ah-close",
            line=Decimal("-0.75"),
            decimal_odds=Decimal("1.95"),
            retrieved_at=RETRIEVED + timedelta(minutes=5),
            raw_payload={"v": 2},
            settlement=SettlementFingerprint(
                scope=SettlementScope.REGULATION_TIME,
                period=FootballPeriod.FULL_TIME,
                line=Decimal("-0.75"),
                extra_time_included=False,
                penalties_included=False,
                push_possible=True,
            ),
        ),
        _record(
            quote_type=QuoteType.OPENING,
            source_market_id="ah-ps-open",
            source_reference="ah-ps-open",
            bookmaker="ps",
            venue="ps",
            decimal_odds=Decimal("1.88"),
        ),
        _record(
            quote_type=QuoteType.CLOSING,
            source_market_id="ah-ps-close",
            source_reference="ah-ps-close",
            bookmaker="ps",
            venue="ps",
            decimal_odds=Decimal("2.02"),
        ),
    ]
    repository = SqliteOddsRepository()
    service = OddsIngestionService(repository)
    first = service.ingest_records("synthetic", records)
    replay = service.ingest_records("synthetic", records)
    stored = repository.list_observations()
    assert first.observations_created == 5
    assert replay.observations_created == 0
    assert len(stored) == 5
    classified = classify_open_close(stored)
    assert len(classified.price_moves) == 1
    ah_shifts = [item for item in classified.line_shifts if item.market_family == "asian_handicap"]
    assert len(ah_shifts) == 1
    assert repository.count_same_line_opening_closing_pairs() == len(classified.price_moves)
    assert repository.count_asian_handicap_line_shifts() == len(ah_shifts)
    repository.close()


def test_coverage_counters_match_classifier_and_ignore_invalid_cross_pairs() -> None:
    repository = SqliteOddsRepository()
    service = OddsIngestionService(repository)
    valid_open = _record(quote_type=QuoteType.OPENING, decimal_odds=Decimal("1.90"))
    valid_close = _record(
        quote_type=QuoteType.CLOSING,
        source_market_id="ah-close-same",
        decimal_odds=Decimal("2.05"),
    )
    shift_open = _record(
        quote_type=QuoteType.OPENING,
        source_market_id="ah-shift-open",
        bookmaker="ps",
        venue="ps",
        line=Decimal("-1.5"),
        decimal_odds=Decimal("1.85"),
        settlement=SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            line=Decimal("-1.5"),
            extra_time_included=False,
            penalties_included=False,
            push_possible=True,
        ),
    )
    shift_close = _record(
        quote_type=QuoteType.CLOSING,
        source_market_id="ah-shift-close",
        bookmaker="ps",
        venue="ps",
        line=Decimal("-1.75"),
        decimal_odds=Decimal("1.88"),
        settlement=SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            line=Decimal("-1.75"),
            extra_time_included=False,
            penalties_included=False,
            push_possible=True,
        ),
    )
    cross_book_close = _record(
        quote_type=QuoteType.CLOSING,
        source_market_id="ah-max",
        bookmaker="max",
        venue="max",
        decimal_odds=Decimal("2.10"),
    )
    incomplete_close = _record(
        quote_type=QuoteType.CLOSING,
        source_market_id="ah-incomplete-close",
        decimal_odds=Decimal("2.00"),
        semantics_complete=False,
        settlement=SettlementFingerprint(period=FootballPeriod.FULL_TIME, line=Decimal("-0.5")),
    )
    records = [valid_open, valid_close, shift_open, shift_close, cross_book_close, incomplete_close]
    first = service.ingest_records("synthetic", records)
    replay = service.ingest_records("synthetic", records)
    assert first.observations_created == 6
    assert replay.observations_created == 0
    stored = repository.list_observations()
    classified = classify_open_close(stored)
    assert len(classified.price_moves) == 1
    assert classified.price_moves[0].bookmaker == "b365"
    ah_shifts = [item for item in classified.line_shifts if item.market_family == "asian_handicap"]
    assert len(ah_shifts) == 1
    assert ah_shifts[0].opening_line == Decimal("-1.5")
    assert ah_shifts[0].closing_line == Decimal("-1.75")
    assert repository.count_same_line_opening_closing_pairs() == len(classified.price_moves)
    assert repository.count_asian_handicap_line_shifts() == len(ah_shifts)
    repository.close()


def test_newer_invalidated_or_priceless_correction_suppresses_older_pair() -> None:
    opening = _record(
        quote_type=QuoteType.OPENING,
        source_market_id="ah-open",
        source_reference="ah-open",
        decimal_odds=Decimal("1.90"),
    )
    valid_close = _record(
        quote_type=QuoteType.CLOSING,
        source_market_id="ah-close",
        source_reference="ah-close",
        decimal_odds=Decimal("2.00"),
        retrieved_at=RETRIEVED,
        raw_payload={"v": 1},
    )
    invalidated = _record(
        quote_type=QuoteType.CLOSING,
        source_market_id="ah-close",
        source_reference="ah-close",
        decimal_odds=Decimal("2.00"),
        retrieved_at=RETRIEVED + timedelta(minutes=5),
        semantics_complete=False,
        settlement=SettlementFingerprint(period=FootballPeriod.FULL_TIME, line=Decimal("-0.5")),
        raw_payload={"v": 2},
    )
    priceless = _record(
        quote_type=QuoteType.CLOSING,
        source_market_id="ah-close",
        source_reference="ah-close",
        decimal_odds=None,
        retrieved_at=RETRIEVED + timedelta(minutes=10),
        raw_payload={"v": 3},
    )
    mapped = [map_raw_record(opening), map_raw_record(valid_close), map_raw_record(invalidated)]
    assert revision_identity_key(mapped[1]) == revision_identity_key(mapped[2])
    classified = classify_open_close(mapped)
    assert classified.price_moves == ()
    assert classified.line_shifts == ()

    repository = SqliteOddsRepository()
    service = OddsIngestionService(repository)
    first = service.ingest_records("synthetic", [opening, valid_close, invalidated])
    replay = service.ingest_records("synthetic", [opening, valid_close, invalidated])
    stored = repository.list_observations()
    assert first.observations_created == 3
    assert replay.observations_created == 0
    assert len(stored) == 3
    stored_classified = classify_open_close(stored)
    assert stored_classified.price_moves == ()
    assert repository.count_same_line_opening_closing_pairs() == 0
    assert repository.count_asian_handicap_line_shifts() == 0

    priceless_result = service.ingest_records("synthetic", [priceless])
    assert priceless_result.observations_created == 1
    after_priceless = repository.list_observations()
    assert len(after_priceless) == 4
    assert classify_open_close(after_priceless).price_moves == ()
    assert repository.count_same_line_opening_closing_pairs() == 0
    repository.close()


def test_duplicate_source_identities_same_proposition_count_once() -> None:
    opening = _record(
        quote_type=QuoteType.OPENING,
        source_market_id="ah-open",
        source_reference="ah-open",
        decimal_odds=Decimal("1.90"),
    )
    older_close = _record(
        quote_type=QuoteType.CLOSING,
        source_market_id="ah-close-a",
        source_reference="ah-close-a",
        decimal_odds=Decimal("2.00"),
        retrieved_at=RETRIEVED,
        raw_payload={"stream": "a"},
    )
    newer_close = _record(
        quote_type=QuoteType.CLOSING,
        source_market_id="ah-close-b",
        source_reference="ah-close-b",
        decimal_odds=Decimal("2.15"),
        retrieved_at=RETRIEVED + timedelta(minutes=5),
        raw_payload={"stream": "b"},
    )
    mapped_older = map_raw_record(older_close)
    mapped_newer = map_raw_record(newer_close)
    assert revision_identity_key(mapped_older) != revision_identity_key(mapped_newer)
    assert open_close_pair_key(mapped_older) == open_close_pair_key(mapped_newer)

    classified = classify_open_close(
        [map_raw_record(opening), mapped_older, mapped_newer]
    )
    assert len(classified.price_moves) == 1
    assert classified.price_moves[0].closing_odds == Decimal("2.15")
    assert classified.line_shifts == ()

    repository = SqliteOddsRepository()
    service = OddsIngestionService(repository)
    records = [opening, older_close, newer_close]
    first = service.ingest_records("synthetic", records)
    replay = service.ingest_records("synthetic", records)
    stored = repository.list_observations()
    assert first.observations_created == 3
    assert replay.observations_created == 0
    assert len(stored) == 3
    stored_classified = classify_open_close(stored)
    assert len(stored_classified.price_moves) == 1
    assert stored_classified.price_moves[0].closing_odds == Decimal("2.15")
    assert repository.count_same_line_opening_closing_pairs() == 1
    assert repository.count_asian_handicap_line_shifts() == 0
    repository.close()
