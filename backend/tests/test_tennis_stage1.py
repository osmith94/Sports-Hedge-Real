"""Tennis Stage 1: ATP/WTA singles identity, catalogue pricing, non-executable winner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
)
from sports_hedge.application.catalogue_maintenance import (
    family_key_from_kalshi_series,
    pair_identity_from_markets,
)
from sports_hedge.application.collector import (
    DEFAULT_PROVIDER_CONCURRENCY,
    matchbook_scope_discovery_params,
)
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.price_engine import CataloguePriceEngine, PriceEnginePriority
from sports_hedge.application.target_competitions import (
    OPERATOR_COMPETITION_REGISTRY_VERSION,
    PRINCIPAL_OPERATOR_COMPETITION_COUNT,
    TargetCompetitionCode,
    default_operator_competition_code_values,
    operator_competition_catalog,
    resolve_target_competition_from_kalshi_ticker,
    scope_kalshi_event,
    scope_matchbook_event,
    scope_polymarket_event,
    selected_includes_soccer,
    selected_includes_tennis,
)
from sports_hedge.catalogue.admission import assess_catalogue_admission
from sports_hedge.domain.football import CanonicalOutcome
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import registered_canonical_key
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.matching.paper_assumed import (
    OWNER_APPROVED_PAPER_EQUIVALENCE_REASON,
    paper_assumed_solver_model,
)
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
)
from sports_hedge.tennis.constants import (
    CANONICAL_TENNIS_MATCH_WINNER,
    TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT,
)
from sports_hedge.venues.matchbook import select_tennis_sport_id

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
DRIFT = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)


def _mb_event(*, name: str, start: str, competition: str, round_name: str, event_id: str = "3440") -> dict:
    return {
        "id": event_id,
        "name": name,
        "start": start,
        "meta-tags": [
            {"type": "SPORT", "name": "Tennis"},
            {"type": "COMPETITION", "name": competition},
            {"type": "OTHER", "name": round_name},
        ],
    }


def _mb_market(event_id: str = "3440") -> dict:
    return {
        "id": "9001",
        "event-id": event_id,
        "name": "Moneyline",
        "market-type": "money_line",
        "runners": [
            {"id": "11", "name": "Mattia Bellucci"},
            {"id": "12", "name": "Kamil Majchrzak"},
        ],
    }


def _kalshi_event(
    *,
    title: str = "Bellucci vs Majchrzak",
    competition: str = "ATP Hangzhou",
    rules: str = "R1",
    occurrence: str = "2026-09-22T05:00:00Z",
    left: str = "Mattia Bellucci",
    right: str = "Kamil Majchrzak",
    series: str = "KXATPMATCH",
) -> dict:
    return {
        "event_ticker": f"{series}-26SEP24",
        "series_ticker": series,
        "title": title,
        "product_metadata": {"competition": competition, "competition_scope": "Game"},
        "markets": [
            {
                "ticker": f"{series}-26SEP24-L",
                "yes_sub_title": left,
                "rules_primary": f"If {left} wins the match in the {rules} after a ball has been played, then the market resolves to Yes.",
                "rules_secondary": "If the match does not occur, resolve to a fair price.",
                "occurrence_datetime": occurrence,
            },
            {
                "ticker": f"{series}-26SEP24-R",
                "yes_sub_title": right,
                "rules_primary": rules,
                "occurrence_datetime": occurrence,
            },
        ],
    }


def _pair_markets():
    mb_event = MatchbookNormalizer().normalize_event(
        _mb_event(
            name="Kamil Majchrzak vs Mattia Bellucci",
            start="2026-09-24T09:00:00Z",
            competition="ATP Hangzhou",
            round_name="R1",
        )
    )
    mb = MatchbookNormalizer().normalize_market(mb_event, _mb_market())
    kalshi_payload = _kalshi_event()
    k_event = KalshiNormalizer().normalize_event(kalshi_payload)
    k_markets = KalshiNormalizer().assemble_canonical_markets(
        k_event, kalshi_payload["markets"], event_payload=kalshi_payload
    )
    return mb, k_markets[0]


def test_reversed_participant_order_clusters_and_maps_winner() -> None:
    forward = MatchbookNormalizer().normalize_event(
        _mb_event(
            name="Mattia Bellucci vs Kamil Majchrzak",
            start="2026-09-24T09:00:00Z",
            competition="ATP Hangzhou",
            round_name="R1",
            event_id="1",
        )
    )
    reverse = MatchbookNormalizer().normalize_event(
        _mb_event(
            name="Kamil Majchrzak vs Mattia Bellucci",
            start="2026-09-26T11:00:00Z",
            competition="Hangzhou Open",
            round_name="R1",
            event_id="2",
        )
    )
    reverse = reverse.model_copy(update={"source_venue": VenueName.KALSHI, "source_event_id": "k-2"})
    result = EventMatcher().match(forward, reverse)
    assert result.matched is True
    assert result.confidence == 1.0
    assert "schedule_drift_within_supporting_window" in result.reasons
    swapped = reverse.model_copy(
        update={"home_team": reverse.away_team, "away_team": reverse.home_team}
    )
    swapped_result = EventMatcher().match(forward, swapped)
    assert swapped_result.matched is True
    assert "participant_order_reversed" in swapped_result.reasons
    assert "kickoff_outside_tolerance" not in result.reasons
    assert forward.home_team == reverse.home_team
    left_market = MatchbookNormalizer().normalize_market(forward, _mb_market())
    right_payload = {
        "id": "9002",
        "name": "Moneyline",
        "market-type": "money_line",
        "runners": [
            {"id": "21", "name": "Kamil Majchrzak"},
            {"id": "22", "name": "Mattia Bellucci"},
        ],
    }
    right_market = MatchbookNormalizer().normalize_market(reverse, right_payload)
    right_market = right_market.model_copy(update={"source_venue": VenueName.KALSHI})
    matched = MarketMatcher().match(left_market, right_market)
    assert matched.matched is True
    by_outcome = {runner.outcome: runner.label for runner in right_market.runners}
    assert by_outcome[CanonicalOutcome.HOME] == "Kamil Majchrzak"
    assert by_outcome[CanonicalOutcome.AWAY] == "Mattia Bellucci"
    assert by_outcome == {runner.outcome: runner.label for runner in left_market.runners}


def test_similar_surnames_do_not_merge() -> None:
    left = MatchbookNormalizer().normalize_event(
        _mb_event(
            name="Gabriela Ruse vs Sofia Kenin",
            start="2026-09-24T09:00:00Z",
            competition="WTA Seoul",
            round_name="R1",
        )
    )
    right = MatchbookNormalizer().normalize_event(
        _mb_event(
            name="Elena-Gabriela Ruse vs Sofia Kenin",
            start="2026-09-24T09:00:00Z",
            competition="WTA Seoul",
            round_name="R1",
            event_id="other",
        )
    )
    right = right.model_copy(update={"source_venue": VenueName.POLYMARKET})
    result = EventMatcher().match(left, right)
    assert result.matched is False
    assert "tennis_player_mismatch" in result.reasons


def test_surname_only_label_fails_closed() -> None:
    payload = _kalshi_event(left="Bellucci", right="Majchrzak")
    with pytest.raises(Exception, match="tennis_player_identity_ambiguous"):
        KalshiNormalizer().normalize_event(payload)


def test_schedule_drift_keeps_same_match_and_separates_unrelated() -> None:
    mb, kalshi = _pair_markets()
    assert abs(mb.event.kickoff_utc - kalshi.event.kickoff_utc) > timedelta(minutes=5)
    assert EventMatcher().match(mb.event, kalshi.event).matched is True
    other = kalshi.event.model_copy(
        update={
            "tournament": "atp chengdu",
            "source_event_id": "other-event",
            "source_venue": VenueName.POLYMARKET,
        }
    )
    assert EventMatcher().match(mb.event, other).matched is False
    later = kalshi.event.model_copy(
        update={"kickoff_utc": mb.event.kickoff_utc + timedelta(days=20)}
    )
    later_result = EventMatcher().match(mb.event, later)
    assert later_result.matched is False
    assert "tennis_schedule_outside_supporting_window" in later_result.reasons


def test_round_vocabularies_are_not_equated() -> None:
    same_round_mb, _kalshi = _pair_markets()
    round_of_32 = _kalshi_event(rules="Round Of 32")
    event = KalshiNormalizer().normalize_event(round_of_32)
    result = EventMatcher().match(same_round_mb.event, event)
    assert result.matched is False
    assert "tennis_round_mismatch" in result.reasons


def test_atp_wta_and_singles_boundaries_fail_closed() -> None:
    atp = MatchbookNormalizer().normalize_event(
        _mb_event(
            name="Mattia Bellucci vs Kamil Majchrzak",
            start="2026-09-24T09:00:00Z",
            competition="ATP Hangzhou",
            round_name="R1",
        )
    )
    wta = atp.model_copy(update={"competition": "WTA", "source_venue": VenueName.KALSHI})
    assert EventMatcher().match(atp, wta).matched is False
    doubles = atp.model_copy(update={"event_type": "doubles", "source_venue": VenueName.POLYMARKET})
    assert "tennis_event_type_not_singles" in EventMatcher().match(atp, doubles).reasons
    missing_round = atp.model_copy(update={"round_label": "", "source_event_id": "no-round"})
    missing_round = missing_round.model_copy(update={"source_venue": VenueName.KALSHI})
    assert "tennis_round_unavailable" in EventMatcher().match(atp, missing_round).reasons
    challenger = scope_matchbook_event(
        _mb_event(
            name="A Player vs B Player",
            start="2026-09-24T09:00:00Z",
            competition="ATP Genoa Challenger",
            round_name="R1",
        ),
        selected_codes=["atp"],
    )
    assert challenger.allowed is False
    pm = scope_polymarket_event(
        {
            "id": "9",
            "title": "Phan Thiet 4: Player One vs Player Two",
            "series": [{"id": "10365"}],
            "eventMetadata": {"league": "Phan Thiet 4"},
        },
        selected_codes=["atp"],
    )
    assert pm.allowed is False
    assert pm.reason == "tennis_tournament_not_admitted"
    game = resolve_target_competition_from_kalshi_ticker("KXATPGAME-26SEP24")
    assert game is None
    assert family_key_from_kalshi_series("KXATPGAME") is None
    assert family_key_from_kalshi_series("KXATPMATCH-26SEP24") == CANONICAL_TENNIS_MATCH_WINNER
    kalshi_scope = scope_kalshi_event(
        {"series_ticker": "KXATPMATCH", "title": "Bellucci vs Majchrzak"},
        selected_codes=["atp"],
    )
    assert kalshi_scope.allowed is False
    assert kalshi_scope.reason == "tennis_tournament_not_admitted"


def test_match_winner_is_paper_admitted_without_retirement_block() -> None:
    left, right = _pair_markets()
    assert registered_canonical_key(left, right) == CANONICAL_TENNIS_MATCH_WINNER
    matched = MarketMatcher().match(left, right)
    assert matched.matched is True
    assert TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT not in matched.reasons
    assert OWNER_APPROVED_PAPER_EQUIVALENCE_REASON in matched.reasons
    admission = assess_catalogue_admission(left, right)
    assert admission.allowed is True
    assert admission.paper_mode_admitted is True
    assert admission.live_execution_eligible is False
    assert admission.rejection_reason is None
    assert paper_assumed_solver_model(left, right) == "simple_complete_set"
    assert scan_eligible_pair(left, right, matched) is True
    identity = pair_identity_from_markets(left, right)
    assert identity is not None
    assert identity.register_canonical_key == CANONICAL_TENNIS_MATCH_WINNER


def test_middle_name_alias_is_evidence_backed_only() -> None:
    left = MatchbookNormalizer().normalize_event(
        _mb_event(
            name="Jie Cui vs Adolfo Daniel Vallejo",
            start="2026-09-24T04:00:00Z",
            competition="ATP Hangzhou",
            round_name="R1",
        )
    )
    right = MatchbookNormalizer().normalize_event(
        _mb_event(
            name="Adolfo Vallejo vs Jie Cui",
            start="2026-09-24T06:00:00Z",
            competition="Hangzhou Open",
            round_name="R1",
            event_id="pm",
        )
    )
    right = right.model_copy(update={"source_venue": VenueName.POLYMARKET})
    assert EventMatcher().match(left, right).matched is True
    assert left.home_team == right.home_team


def test_universe_prices_depth_for_paper_tennis_match_winner() -> None:
    left, right = _pair_markets()
    matched = MarketMatcher().match(left, right)
    assert scan_eligible_pair(left, right, matched) is True
    from sports_hedge.application.collector import _kalshi_markets_needing_depth, _NormalizedMarket

    kalshi_leg = _NormalizedMarket(raw={}, canonical=right)
    pair = (
        VenueName.MATCHBOOK,
        VenueName.KALSHI,
        _NormalizedMarket(raw={}, canonical=left),
        kalshi_leg,
        matched,
    )
    assert _kalshi_markets_needing_depth([pair]) == [kalshi_leg]


def test_provider_concurrency_and_tennis_discovery_scope() -> None:
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.POLYMARKET] == 8
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    assert matchbook_scope_discovery_params(["premier_league"]) == {}
    params = matchbook_scope_discovery_params(["atp"], tennis_sport_id="9")
    assert params["sport-ids"] == "9"
    assert "tag-ids" not in params
    sports = [
        {"id": 9, "name": "Tennis"},
        {"id": 1389388027310017, "name": "Table Tennis"},
    ]
    assert select_tennis_sport_id(sports) == 9
    catalog = {row["code"]: row for row in operator_competition_catalog()}
    assert len(catalog) == PRINCIPAL_OPERATOR_COMPETITION_COUNT == 37
    assert OPERATOR_COMPETITION_REGISTRY_VERSION == 9
    assert catalog["atp"]["selectable"] is True
    assert catalog["atp"]["paper_executable"] is True
    assert catalog["wta"]["paper_executable"] is True
    assert catalog["atp"]["default_selected"] is False
    assert "atp" not in default_operator_competition_code_values()
    assert selected_includes_tennis(["atp"]) is True
    assert selected_includes_soccer(["atp"]) is False
    assert selected_includes_soccer(["premier_league"]) is True
    assert TargetCompetitionCode.ATP.value == "atp"


def test_polymarket_non_moneyline_is_not_assembled() -> None:
    event_payload = {
        "id": "pm-1",
        "title": "Hangzhou Open: Adolfo Vallejo vs Jie Cui",
        "startTime": "2026-09-24T04:00:00Z",
        "sport": {"sport": "atp", "series": "10365"},
        "eventMetadata": {"league": "Hangzhou Open"},
        "markets": [
            {
                "id": "ml",
                "sportsMarketType": "moneyline",
                "question": "R1 moneyline",
                "outcomes": '["Adolfo Vallejo", "Jie Cui"]',
                "clobTokenIds": '["tok-home", "tok-away"]',
            }
        ],
    }
    event = PolymarketNormalizer().normalize_event(event_payload)
    markets = PolymarketNormalizer().assemble_canonical_markets(
        event,
        [
            event_payload["markets"][0],
            {"id": "spread", "sportsMarketType": "spreads", "question": "Game handicap"},
        ],
    )
    assert len(markets) == 1
    assert markets[0].family.value == "game_winner"
    assert {runner.source_runner_id for runner in markets[0].runners} == {"tok-home", "tok-away"}


@pytest.mark.asyncio
async def test_background_prices_approved_tennis_row_without_rediscovery() -> None:
    from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
    from sports_hedge.application.provider_access import ProviderAccessLayer
    from sports_hedge.persistence.approved_market_catalogue import (
        SqliteApprovedMarketCatalogueStore,
    )

    row = ApprovedMarketCatalogueRow(
        catalogue_row_id="amc-tennis",
        register_canonical_key=CANONICAL_TENNIS_MATCH_WINNER,
        canonical_event_id="evt-tennis",
        competition="ATP",
        home_canonical="mattia bellucci",
        away_canonical="kamil majchrzak",
        kickoff_utc=NOW + timedelta(days=10),
        matchbook_event_id="3440",
        matchbook_market_id="9001",
        matchbook_runner_ids=[
            OutcomeNativeId(outcome="home", native_id="11"),
            OutcomeNativeId(outcome="away", native_id="12"),
        ],
        kalshi_event_ticker="KXATPMATCH-26SEP24",
        kalshi_market_tickers=["KXATPMATCH-26SEP24-L", "KXATPMATCH-26SEP24-R"],
        kalshi_outcome_ids=[
            OutcomeNativeId(outcome="home", native_id="KXATPMATCH-26SEP24-L:YES"),
            OutcomeNativeId(outcome="away", native_id="KXATPMATCH-26SEP24-R:YES"),
        ],
        family="game_winner",
        period="full_time",
        required_outcomes=["home", "away"],
        row_state=CatalogueRowState.ACTIVE,
        first_catalogued_at=NOW,
        last_confirmed_at=NOW,
        content_version=1,
    )

    class Matchbook:
        def __init__(self) -> None:
            self.list_events_calls = 0
            self.list_markets_calls: list[str] = []
            self.get_market_calls: list[tuple[str, str]] = []

        async def list_events(self, **_filters: object) -> dict:
            self.list_events_calls += 1
            return {"events": []}

        async def list_markets(self, event_id: str, **_filters: object) -> dict:
            self.list_markets_calls.append(str(event_id))
            return {"markets": []}

        async def get_market(self, event_id: str, market_id: str, **_filters: object) -> dict:
            self.get_market_calls.append((str(event_id), str(market_id)))
            return {
                "id": market_id,
                "name": "Moneyline",
                "status": "open",
                "runners": [
                    {"id": 11, "name": "Mattia Bellucci", "prices": [{"odds": "1.80", "side": "back"}]},
                    {"id": 12, "name": "Kamil Majchrzak", "prices": [{"odds": "2.10", "side": "back"}]},
                ],
            }

    class Kalshi:
        def __init__(self) -> None:
            self.list_events_calls = 0
            self.list_markets_calls: list[str] = []
            self.book_calls: list[str] = []

        async def list_events(self, **_filters: object) -> dict:
            self.list_events_calls += 1
            return {"events": []}

        async def list_markets(self, event_id: str, **_filters: object) -> dict:
            self.list_markets_calls.append(str(event_id))
            return {"markets": []}

        async def get_order_book(self, _event_id: str, market_id: str, **_filters: object) -> dict:
            self.book_calls.append(str(market_id))
            return {"orderbook_fp": {"yes_dollars": [["0.40", "10"]], "no_dollars": [["0.55", "10"]]}}

    store = SqliteApprovedMarketCatalogueStore(":memory:")
    store.upsert_catalogue_row(row)
    matchbook = Matchbook()
    kalshi = Kalshi()
    engine = CataloguePriceEngine(
        catalogue_store=store,
        matchbook=matchbook,
        kalshi=kalshi,
        paper_scan=None,
        fixture_state=FixtureCurrentStateStore(),
        provider_access=ProviderAccessLayer(
            {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
        ),
        clock=lambda: NOW,
        provider_timeout_seconds=2,
        hot_interval_seconds=0,
        background_interval_seconds=0,
    )
    engine.reconstruct()
    identity = engine.item("amc-tennis")
    assert identity is not None
    assert engine.classify_priority(identity.identity) is PriceEnginePriority.BACKGROUND
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert matchbook.list_events_calls == 0
    assert matchbook.list_markets_calls == []
    assert kalshi.list_events_calls == 0
    assert kalshi.list_markets_calls == []
    assert matchbook.get_market_calls == [("3440", "9001")]
    assert set(kalshi.book_calls) == {"KXATPMATCH-26SEP24-L", "KXATPMATCH-26SEP24-R"}
    assert "amc-tennis" in result.evaluated or "amc-tennis" in result.statuses()
