from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import MarketSide
from sports_hedge.facts.identity import KickoffPrecision
from sports_hedge.odds.models import QuoteType, RawOddsRecord, VenueKind


def _retrieved() -> datetime:
    return datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _league_settlement(line: Decimal | None = None) -> SettlementFingerprint:
    return SettlementFingerprint(
        scope=SettlementScope.REGULATION_TIME,
        period=FootballPeriod.FULL_TIME,
        line=line,
        push_possible=False,
        penalties_included=False,
        extra_time_included=False,
    )


class SyntheticOddsAdapter:
    """In-repo fixture covering PL, Championship and La Liga 2025/26."""

    source = "synthetic"
    required = False

    def fetch(self) -> Sequence[RawOddsRecord]:
        retrieved = _retrieved()
        records: list[RawOddsRecord] = []
        records.extend(_premier_league(retrieved))
        records.extend(_championship(retrieved))
        records.extend(_la_liga(retrieved))
        return records


def _premier_league(retrieved: datetime) -> list[RawOddsRecord]:
    kickoff = datetime(2025, 8, 16, 14, 0, tzinfo=UTC)
    later = kickoff - timedelta(hours=2)
    records: list[RawOddsRecord] = []
    # Timestamped exchange 1X2 + liquidity (tier A)
    for selection, odds, liquidity in (
        ("home", "2.10", "1200"),
        ("draw", "3.40", "800"),
        ("away", "3.80", "900"),
    ):
        records.append(
            RawOddsRecord(
                source="synthetic",
                source_market_id="syn-pl-ars-liv-1x2",
                source_match_id="syn-pl-ars-liv",
                source_reference="synthetic:pl:arsenal-chelsea:1x2:timestamped",
                venue="synthetic_exchange",
                venue_kind=VenueKind.EXCHANGE,
                competition="Premier League",
                season="2025/26",
                home_team="Arsenal",
                                away_team="Chelsea",
                kickoff_utc=kickoff,
                kickoff_precision=KickoffPrecision.MINUTE,
                market_family=MarketFamily.MATCH_RESULT,
                period=FootballPeriod.FULL_TIME,
                selection=selection,
                side=MarketSide.BACK,
                decimal_odds=Decimal(odds),
                observed_at=later,
                quote_type=QuoteType.TIMESTAMPED,
                liquidity=Decimal(liquidity),
                commission_known=True,
                retrieved_at=retrieved,
                settlement=_league_settlement(),
                semantics_complete=True,
                raw_payload={"kind": "timestamped_exchange"},
            )
        )
    # Closing-only 1X2 for the same match (tier C) — must coexist
    for selection, odds in (("home", "2.05"), ("draw", "3.50"), ("away", "3.70")):
        records.append(
            RawOddsRecord(
                source="synthetic",
                source_market_id="syn-pl-ars-liv-1x2-close",
                source_match_id="syn-pl-ars-liv",
                source_reference="synthetic:pl:arsenal-chelsea:1x2:closing",
                venue="synthetic_book",
                bookmaker="synthetic_book",
                venue_kind=VenueKind.BOOKMAKER,
                competition="Premier League",
                season="2025/26",
                home_team="Arsenal",
                                away_team="Chelsea",
                kickoff_utc=kickoff,
                kickoff_precision=KickoffPrecision.MINUTE,
                market_family=MarketFamily.MATCH_RESULT,
                period=FootballPeriod.FULL_TIME,
                selection=selection,
                decimal_odds=Decimal(odds),
                observed_at=None,
                quote_type=QuoteType.CLOSING,
                retrieved_at=retrieved,
                settlement=_league_settlement(),
                semantics_complete=True,
                raw_payload={"kind": "closing_only"},
            )
        )
    # Totals + BTTS timestamped bookmaker (tier B) — no liquidity claimed
    for selection, odds in (("over", "1.90"), ("under", "1.95")):
        records.append(
            RawOddsRecord(
                source="synthetic",
                source_market_id="syn-pl-ars-liv-ou25",
                source_match_id="syn-pl-ars-liv",
                source_reference="synthetic:pl:arsenal-chelsea:ou25",
                venue="synthetic_book",
                bookmaker="synthetic_book",
                venue_kind=VenueKind.BOOKMAKER,
                competition="Premier League",
                season="2025/26",
                home_team="Arsenal",
                                away_team="Chelsea",
                kickoff_utc=kickoff,
                kickoff_precision=KickoffPrecision.MINUTE,
                market_family=MarketFamily.TOTAL_GOALS,
                period=FootballPeriod.FULL_TIME,
                line=Decimal("2.5"),
                selection=selection,
                decimal_odds=Decimal(odds),
                observed_at=later,
                quote_type=QuoteType.TIMESTAMPED,
                retrieved_at=retrieved,
                settlement=_league_settlement(Decimal("2.5")),
                semantics_complete=True,
                raw_payload={"kind": "timestamped_book"},
            )
        )
    for selection, odds in (("yes", "1.80"), ("no", "2.05")):
        records.append(
            RawOddsRecord(
                source="synthetic",
                source_market_id="syn-pl-ars-liv-btts",
                source_match_id="syn-pl-ars-liv",
                source_reference="synthetic:pl:arsenal-chelsea:btts",
                venue="synthetic_book",
                bookmaker="synthetic_book",
                venue_kind=VenueKind.BOOKMAKER,
                competition="Premier League",
                season="2025/26",
                home_team="Arsenal",
                                away_team="Chelsea",
                kickoff_utc=kickoff,
                kickoff_precision=KickoffPrecision.MINUTE,
                market_family=MarketFamily.BOTH_TEAMS_TO_SCORE,
                period=FootballPeriod.FULL_TIME,
                selection=selection,
                decimal_odds=Decimal(odds),
                observed_at=later,
                quote_type=QuoteType.TIMESTAMPED,
                retrieved_at=retrieved,
                settlement=_league_settlement(),
                semantics_complete=True,
                raw_payload={"kind": "timestamped_book"},
            )
        )
    # Incomplete settlement 1X2 — must not masquerade as equivalent
    records.append(
        RawOddsRecord(
            source="synthetic",
            source_market_id="syn-pl-ars-liv-1x2-incomplete",
            source_match_id="syn-pl-ars-liv",
            source_reference="synthetic:pl:arsenal-chelsea:1x2:unknown-settlement",
            venue="synthetic_book",
            venue_kind=VenueKind.BOOKMAKER,
            competition="Premier League",
            season="2025/26",
            home_team="Arsenal",
                            away_team="Chelsea",
            kickoff_utc=kickoff,
            kickoff_precision=KickoffPrecision.MINUTE,
            market_family=MarketFamily.MATCH_RESULT,
            period=FootballPeriod.FULL_TIME,
            selection="home",
            decimal_odds=Decimal("2.20"),
            observed_at=later,
            quote_type=QuoteType.TIMESTAMPED,
            retrieved_at=retrieved,
            settlement=SettlementFingerprint(),
            semantics_complete=False,
            raw_payload={"kind": "incomplete_settlement"},
        )
    )
    # Second PL match: closing 1X2 only
    kickoff_b = datetime(2025, 8, 17, 15, 30, tzinfo=UTC)
    for selection, odds in (("home", "2.60"), ("draw", "3.30"), ("away", "2.80")):
        records.append(
            RawOddsRecord(
                source="synthetic",
                source_market_id="syn-pl-che-mci-1x2-close",
                source_match_id="syn-pl-che-mci",
                source_reference="synthetic:pl:chelsea-leeds:1x2:closing",
                venue="synthetic_book",
                bookmaker="synthetic_book",
                venue_kind=VenueKind.BOOKMAKER,
                competition="Premier League",
                season="2025/26",
                home_team="Chelsea",
                away_team="Leeds United",
                kickoff_utc=kickoff_b,
                kickoff_precision=KickoffPrecision.MINUTE,
                market_family=MarketFamily.MATCH_RESULT,
                period=FootballPeriod.FULL_TIME,
                selection=selection,
                decimal_odds=Decimal(odds),
                quote_type=QuoteType.CLOSING,
                retrieved_at=retrieved,
                settlement=_league_settlement(),
                semantics_complete=True,
                home_goals=1,
                away_goals=1,
                raw_payload={"kind": "closing_only"},
            )
        )
    return records


def _championship(retrieved: datetime) -> list[RawOddsRecord]:
    kickoff = datetime(2025, 8, 9, 14, 0, tzinfo=UTC)
    # Facts only / unavailable odds (tier D)
    return [
        RawOddsRecord(
            source="synthetic",
            source_market_id="syn-ch-lee-lei-facts",
            source_match_id="syn-ch-lee-lei",
            source_reference="synthetic:ch:leeds-leicester:facts",
            competition="Championship",
            season="2025/26",
            home_team="Leeds United",
            away_team="Leicester City",
            kickoff_utc=kickoff,
            kickoff_precision=KickoffPrecision.MINUTE,
            market_family=MarketFamily.MATCH_RESULT,
            period=FootballPeriod.FULL_TIME,
            selection="unavailable",
            decimal_odds=None,
            quote_type=QuoteType.UNKNOWN,
            retrieved_at=retrieved,
            settlement=_league_settlement(),
            semantics_complete=True,
            home_goals=2,
            away_goals=1,
            raw_payload={"kind": "facts_only"},
        )
    ]


def _la_liga(retrieved: datetime) -> list[RawOddsRecord]:
    kickoff = datetime(2025, 10, 26, 19, 0, tzinfo=UTC)
    observed = kickoff - timedelta(minutes=90)
    records: list[RawOddsRecord] = []
    for selection, odds, liq in (("home", "2.40", "5000"), ("draw", "3.60", "2100"), ("away", "3.10", "3300")):
        records.append(
            RawOddsRecord(
                source="synthetic",
                source_market_id="syn-ll-rma-bar-1x2",
                source_match_id="syn-ll-rma-bar",
                source_reference="synthetic:ll:madrid-barcelona:1x2:timestamped",
                venue="synthetic_exchange",
                venue_kind=VenueKind.EXCHANGE,
                competition="La Liga",
                season="2025/26",
                home_team="Real Madrid",
                away_team="Barcelona",
                kickoff_utc=kickoff,
                kickoff_precision=KickoffPrecision.MINUTE,
                market_family=MarketFamily.MATCH_RESULT,
                period=FootballPeriod.FULL_TIME,
                selection=selection,
                side=MarketSide.BACK,
                decimal_odds=Decimal(odds),
                observed_at=observed,
                quote_type=QuoteType.TIMESTAMPED,
                liquidity=Decimal(liq),
                spread=Decimal("0.04"),
                commission_known=True,
                retrieved_at=retrieved,
                settlement=_league_settlement(),
                semantics_complete=True,
                raw_payload={"kind": "timestamped_exchange"},
            )
        )
    return records
