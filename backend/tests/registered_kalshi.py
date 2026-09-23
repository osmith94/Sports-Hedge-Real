"""Registered Matchbook↔Kalshi counterparts for production-path tests.

Polymarket remains valid offline census knowledge. Runtime PAPER comparison
only admits Approved Match Register rows, so helpers here build Kalshi GAME /
BTTS / TOTAL / FTTS observations and a collector client. Deterministic
fixture/demo payloads. Not owner-live quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_kalshi_costs, profit_commission_cost

REGULATION = (
    "Resolves on 90 minutes of regulation time. Extra time and penalties do not count."
)
ARB_BOOK = {
    "orderbook_fp": {
        "yes_dollars": [["0.20", "500.00"]],
        "no_dollars": [["0.70", "500.00"]],
    }
}
FAIR_BOOK = {
    "orderbook_fp": {
        "yes_dollars": [["0.50", "200.00"]],
        "no_dollars": [["0.50", "200.00"]],
    }
}


def teams_from_matchbook_event(event: dict[str, Any]) -> tuple[str, str]:
    name = str(event.get("name") or event.get("title") or "")
    if " vs " not in name:
        raise ValueError(f"cannot parse teams from {event!r}")
    home, away = name.split(" vs ", 1)
    return home.strip(), away.strip()


def teams_from_title(title: str) -> tuple[str, str]:
    normalized = (
        str(title)
        .replace(" vs. ", " vs ")
        .replace(" v. ", " vs ")
        .replace(" v ", " vs ")
    )
    if " vs " not in normalized:
        raise ValueError(f"cannot parse teams from {title!r}")
    home, away = normalized.split(" vs ", 1)
    return home.strip(), away.strip()


def kalshi_series(ticker: str, competition: str = "Premier League") -> dict[str, Any]:
    return {
        "ticker": ticker,
        "title": competition,
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Opta"}],
    }


def kalshi_event(
    *,
    home: str,
    away: str,
    kickoff: datetime | str,
    competition: str = "Premier League",
    series_ticker: str = "KXEPLGAME",
    suffix: str = "NEWCHE",
    markets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ticker = f"{series_ticker}-{suffix}"
    stamp = kickoff.isoformat() if isinstance(kickoff, datetime) else str(kickoff)
    return {
        "event_ticker": ticker,
        "series_ticker": series_ticker,
        "title": f"{home} vs {away}",
        "category": "Sports",
        "strike_date": stamp,
        "competition": competition,
        "product_metadata": {"competition": competition},
        "markets": list(markets or []),
    }


def _binary(
    ticker: str,
    event_ticker: str,
    title: str,
    yes_sub_title: str,
    rules: str,
    *,
    strike: str | None = None,
    secondary: str | None = None,
) -> dict[str, Any]:
    payload = {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "title": title,
        "yes_sub_title": yes_sub_title,
        "rules_primary": rules,
        "rules_secondary": secondary,
    }
    if strike is not None:
        payload["strike"] = strike
        payload["title"] = f"{title} Total Goals {strike}"
    return payload


def _books_for(tickers: list[str], *, arb: bool) -> dict[str, dict[str, Any]]:
    template = ARB_BOOK if arb else FAIR_BOOK
    return {ticker: {**template, "orderbook_fp": dict(template["orderbook_fp"])} for ticker in tickers}


def counterpart_kind(market: dict[str, Any], *, home: str = "", away: str = "") -> str:
    """Map a PM-shaped or named market onto a Kalshi native archetype, if any."""

    mtype = str(market.get("sportsMarketType") or "").lower()
    name = str(market.get("name") or market.get("question") or market.get("title") or "").lower()
    blob = f"{mtype} {name}"
    if "draw no bet" in blob or blob.strip() == "dnb":
        return "unregistered"
    if "handicap" in blob:
        return "unregistered"
    if "to qualify" in blob:
        return "unregistered"
    if "first team to score" in blob or "ftts" in blob:
        return "FTTS"
    if "both teams to score" in blob or "btts" in blob:
        return "BTTS"
    if "team total" in blob:
        return "unregistered"
    if " - " in str(market.get("question") or "") and "total" in blob:
        return "unregistered"
    if "total" in blob or "over/under" in blob:
        if home and home.lower() in blob and away.lower() not in blob:
            return "unregistered"
        if away and away.lower() in blob and home.lower() not in blob:
            return "unregistered"
        line = market.get("line") or market.get("strike")
        if line is not None:
            from sports_hedge.domain.football import line_push_possible

            try:
                if line_push_possible(Decimal(str(line))) is not False:
                    return "unregistered"
            except Exception:
                return "unregistered"
        return "TOTAL"
    if "moneyline" in blob or "match result" in blob or "match odds" in blob:
        return "GAME"
    return "unregistered"


def kalshi_payloads_for_counterpart(
    event: dict[str, Any],
    market: dict[str, Any],
    *,
    arb: bool = True,
    home: str | None = None,
    away: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]] | None:
    title = str(event.get("title") or event.get("name") or "")
    parsed_home, parsed_away = home, away
    if not parsed_home or not parsed_away:
        try:
            parsed_home, parsed_away = teams_from_title(title)
        except ValueError:
            parsed_home, parsed_away = "Home", "Away"
    kind = counterpart_kind(market, home=parsed_home or "", away=parsed_away or "")
    if kind == "unregistered":
        return None
    home = parsed_home or "Home"
    away = parsed_away or "Away"
    kickoff = event.get("startTime") or event.get("start") or datetime.now(UTC).isoformat()
    competition = str(event.get("competition") or event.get("competition-name") or "Premier League")
    rules = str(market.get("description") or market.get("rules_primary") or REGULATION)
    secondary = market.get("rules_secondary")
    line = market.get("line") or market.get("strike")
    if line is None and kind == "TOTAL":
        question = str(market.get("question") or market.get("name") or "")
        for token in question.replace("Over/Under", " ").replace("Goals", " ").split():
            try:
                Decimal(token)
                line = token
                break
            except Exception:
                continue
        line = line or "2.5"
    series_map = {
        "GAME": "KXEPLGAME",
        "BTTS": "KXEPLBTTS",
        "TOTAL": "KXEPLTOTAL",
        "FTTS": "KXEPLFTTS",
    }
    series_ticker = series_map[kind]
    suffix = "REGTEST"
    event_ticker = f"{series_ticker}-{suffix}"
    markets: list[dict[str, Any]]
    if kind == "GAME":
        markets = [
            _binary(f"{event_ticker}-H", event_ticker, title, home, rules, secondary=secondary),
            _binary(f"{event_ticker}-D", event_ticker, title, "Draw", rules, secondary=secondary),
            _binary(f"{event_ticker}-A", event_ticker, title, away, rules, secondary=secondary),
        ]
    elif kind == "BTTS":
        markets = [
            _binary(
                f"{event_ticker}-BTTS",
                event_ticker,
                "Both Teams To Score",
                "Yes",
                rules,
                secondary=secondary,
            )
        ]
    elif kind == "TOTAL":
        markets = [
            _binary(
                f"{event_ticker}-{line}",
                event_ticker,
                title,
                f"Over {line}",
                rules,
                strike=str(line),
                secondary=secondary,
            )
        ]
    else:
        markets = [
            _binary(f"{event_ticker}-H", event_ticker, "First team to score", home, rules, secondary=secondary),
            _binary(f"{event_ticker}-A", event_ticker, "First team to score", away, rules, secondary=secondary),
        ]
        outcomes = str(market.get("outcomes") or "")
        ftts_blob = f"{outcomes} {market.get('question') or ''} {market.get('name') or ''}".casefold()
        if "no goal" in ftts_blob:
            markets.append(
                _binary(
                    f"{event_ticker}-NG",
                    event_ticker,
                    "First team to score",
                    "No Goal",
                    rules,
                    secondary=secondary,
                )
            )
    payload_event = kalshi_event(
        home=home,
        away=away,
        kickoff=kickoff,
        competition=competition,
        series_ticker=series_ticker,
        suffix=suffix,
        markets=markets,
    )
    books = _books_for([item["ticker"] for item in markets], arb=arb)
    return payload_event, markets, books, kalshi_series(series_ticker, competition)


def kalshi_observation_for_counterpart(
    event: dict[str, Any],
    market: dict[str, Any],
    *,
    observed_at: datetime,
    quote_age_ms: int = 80,
    arb: bool = True,
    home: str | None = None,
    away: str | None = None,
) -> VenueMarketObservation | None:
    built = kalshi_payloads_for_counterpart(event, market, arb=arb, home=home, away=away)
    if built is None:
        return None
    payload_event, markets, books, series = built
    try:
        return KalshiObservationBuilder().build(
            payload_event,
            markets,
            books,
            series=series,
            observed_at=observed_at,
            quote_age_ms=quote_age_ms,
            quote_age_basis="retrieval",
            fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
        )
    except Exception:
        return None


def registered_right_observation(
    pm_event: dict[str, Any],
    pm_market: dict[str, Any],
    pm_books: dict[str, dict[str, Any]],
    *,
    observed_at: datetime,
    quote_age_ms: int = 150,
    arb: bool = True,
    matchbook_event: dict[str, Any] | None = None,
) -> VenueMarketObservation:
    home = away = None
    if matchbook_event is not None:
        try:
            home, away = teams_from_matchbook_event(matchbook_event)
        except ValueError:
            home = away = None
    kalshi = kalshi_observation_for_counterpart(
        pm_event,
        pm_market,
        observed_at=observed_at,
        quote_age_ms=80,
        arb=arb,
        home=home,
        away=away,
    )
    if kalshi is not None:
        return kalshi
    return PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=observed_at, quote_age_ms=quote_age_ms
    )


def scan_costs_for(right: VenueMarketObservation, *, captured_at: datetime | None = None):
    if right.market.source_venue is VenueName.KALSHI:
        return matchbook_kalshi_costs(captured_at=captured_at)
    return [
        profit_commission_cost(VenueName.MATCHBOOK, "0.02", captured_at=captured_at),
        profit_commission_cost(VenueName.POLYMARKET, "0", captured_at=captured_at),
    ]


def scan_mb_counterpart(
    mb_event: dict[str, Any],
    mb_market: dict[str, Any],
    pm_event: dict[str, Any],
    pm_market: dict[str, Any],
    pm_books: dict[str, dict[str, Any]],
    *,
    observed_at: datetime,
    arb: bool = True,
    maximum_execution_risk: int = 100,
):
    repository = SqliteMarketIntelligenceRepository()
    service = PaperScanService(MarketIntelligenceService(repository))
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=observed_at, quote_age_ms=120
    )
    right = registered_right_observation(
        pm_event,
        pm_market,
        pm_books,
        observed_at=observed_at,
        arb=arb,
        matchbook_event=mb_event,
    )
    try:
        decision = service.scan_pair(
            matchbook,
            right,
            venue_costs=scan_costs_for(right),
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx"),
                FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency"),
            ],
            maximum_execution_risk=maximum_execution_risk,
        )
        return decision, matchbook, right
    finally:
        repository.close()


SERIES_BY_FAMILY = {
    "GAME": "KXEPLGAME",
    "BTTS": "KXEPLBTTS",
    "TOTAL": "KXEPLTOTAL",
    "FTTS": "KXEPLFTTS",
}


class FakeKalshi:
    """Collector client listing registered GAME/BTTS/TOTAL/FTTS markets per fixture."""

    def __init__(
        self,
        fixtures: list[tuple[str, str, str, datetime]] | list[tuple[str, str, datetime]],
        *,
        families: tuple[str, ...] = ("BTTS",),
        competition: str = "Premier League",
        arb: bool = True,
        latency_s: float = 0.0,
        line: str = "2.5",
    ) -> None:
        self.fixtures = fixtures
        self.families = families
        self.competition = competition
        self.arb = arb
        self.latency_s = latency_s
        self.line = line
        self.list_events_calls = 0
        self.list_markets_calls = 0
        self.book_calls = 0

    def _normalized(self) -> list[tuple[str, str, str, datetime]]:
        rows: list[tuple[str, str, str, datetime]] = []
        for item in self.fixtures:
            if len(item) == 4:
                competition, home, away, kickoff = item
            else:
                home, away, kickoff = item
                competition = self.competition
            rows.append((str(competition), str(home), str(away), kickoff))
        return rows

    def _family_event(
        self,
        *,
        index: int,
        family: str,
        competition: str,
        home: str,
        away: str,
        kickoff: datetime,
    ) -> dict[str, Any]:
        series = SERIES_BY_FAMILY[family]
        suffix = f"26SEP20F{index:02d}{family[:1]}"
        ticker = f"{series}-{suffix}"
        title = f"{home} vs {away}"
        if family == "GAME":
            markets = [
                _binary(f"{ticker}-H", ticker, title, home, REGULATION),
                _binary(f"{ticker}-D", ticker, title, "Draw", REGULATION),
                _binary(f"{ticker}-A", ticker, title, away, REGULATION),
            ]
        elif family == "BTTS":
            markets = [
                _binary(f"{ticker}-BTTS", ticker, "Both Teams To Score", "Yes", REGULATION)
            ]
        elif family == "TOTAL":
            markets = [
                _binary(
                    f"{ticker}-{self.line}",
                    ticker,
                    title,
                    f"Over {self.line}",
                    REGULATION,
                    strike=str(self.line),
                )
            ]
        else:
            markets = [
                _binary(f"{ticker}-H", ticker, "First team to score", home, REGULATION),
                _binary(f"{ticker}-A", ticker, "First team to score", away, REGULATION),
                _binary(f"{ticker}-NG", ticker, "First team to score", "No Goal", REGULATION),
            ]
        return kalshi_event(
            home=home,
            away=away,
            kickoff=kickoff,
            competition=competition,
            series_ticker=series,
            suffix=suffix,
            markets=markets,
        )

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        if self.latency_s:
            import asyncio

            await asyncio.sleep(self.latency_s)
        events: list[dict[str, Any]] = []
        for index, (competition, home, away, kickoff) in enumerate(self._normalized()):
            for family in self.families:
                events.append(
                    self._family_event(
                        index=index,
                        family=family,
                        competition=competition,
                        home=home,
                        away=away,
                        kickoff=kickoff,
                    )
                )
        return {"events": events}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        self.list_markets_calls += 1
        if self.latency_s:
            import asyncio

            await asyncio.sleep(self.latency_s)
        return {"markets": []}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, outcome_id, filters
        self.book_calls += 1
        if self.latency_s:
            import asyncio

            await asyncio.sleep(self.latency_s)
        return dict(ARB_BOOK if self.arb else FAIR_BOOK)

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        return kalshi_series(str(series_ticker), self.competition)


class FakeKalshiBTTS(FakeKalshi):
    """Collector client that lists one BTTS market per fixture identity."""

    def __init__(
        self,
        fixtures: list[tuple[str, str, str, datetime]] | list[tuple[str, str, datetime]],
        *,
        competition: str = "Premier League",
        arb: bool = True,
        latency_s: float = 0.0,
    ) -> None:
        super().__init__(
            fixtures,
            families=("BTTS",),
            competition=competition,
            arb=arb,
            latency_s=latency_s,
        )
