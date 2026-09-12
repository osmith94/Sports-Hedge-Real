from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sports_hedge.domain.football import MarketFamily
from sports_hedge.odds.models import VenueKind

ONE_X_TWO_BOOKS = ("B365", "BW", "IW", "PS", "WH", "VC", "LB", "CL", "BF", "BFD", "BMGM", "BV")
ONE_X_TWO_AGGREGATES = ("Max", "Avg", "BFE")
OVER_UNDER_BOOKS = ("B365", "P")
OVER_UNDER_AGGREGATES = ("Max", "Avg", "BFE")
ASIAN_HANDICAP_BOOKS = ("B365", "P")
ASIAN_HANDICAP_AGGREGATES = ("Max", "Avg", "BFE")


@dataclass(frozen=True, slots=True)
class DeclaredOddsMarket:
    book: str
    family: MarketFamily
    selections: tuple[str, ...]
    opening_columns: tuple[str, ...]
    closing_columns: tuple[str, ...]
    venue_kind: VenueKind
    research_only: bool
    line: Decimal | None = None
    opening_line_column: str | None = None
    closing_line_column: str | None = None


def declared_odds_markets() -> tuple[DeclaredOddsMarket, ...]:
    """Header-driven opening/closing maps for standard Football-Data columns since 2019/20."""

    markets: list[DeclaredOddsMarket] = []
    for book in ONE_X_TWO_BOOKS:
        markets.append(
            DeclaredOddsMarket(
                book=book,
                family=MarketFamily.MATCH_RESULT,
                selections=("home", "draw", "away"),
                opening_columns=(f"{book}H", f"{book}D", f"{book}A"),
                closing_columns=(f"{book}CH", f"{book}CD", f"{book}CA"),
                venue_kind=VenueKind.BOOKMAKER,
                research_only=False,
            )
        )
    for book in ONE_X_TWO_AGGREGATES:
        markets.append(
            DeclaredOddsMarket(
                book=book,
                family=MarketFamily.MATCH_RESULT,
                selections=("home", "draw", "away"),
                opening_columns=(f"{book}H", f"{book}D", f"{book}A"),
                closing_columns=(f"{book}CH", f"{book}CD", f"{book}CA"),
                venue_kind=VenueKind.EXCHANGE if book == "BFE" else VenueKind.AGGREGATE,
                research_only=True,
            )
        )
    ou_specs = (
        *(
            (book, f"{book}>2.5", f"{book}<2.5", f"{book}C>2.5", f"{book}C<2.5", False, VenueKind.BOOKMAKER)
            for book in OVER_UNDER_BOOKS
        ),
        *(
            (book, f"{book}>2.5", f"{book}<2.5", f"{book}C>2.5", f"{book}C<2.5", True, VenueKind.AGGREGATE)
            for book in OVER_UNDER_AGGREGATES
            if book != "BFE"
        ),
        ("BFE", "BFE>2.5", "BFE<2.5", "BFEC>2.5", "BFEC<2.5", True, VenueKind.EXCHANGE),
    )
    for book, open_o, open_u, close_o, close_u, research_only, venue_kind in ou_specs:
        markets.append(
            DeclaredOddsMarket(
                book=book,
                family=MarketFamily.TOTAL_GOALS,
                selections=("over", "under"),
                opening_columns=(open_o, open_u),
                closing_columns=(close_o, close_u),
                venue_kind=venue_kind,
                research_only=research_only,
                line=Decimal("2.5"),
            )
        )
    ah_specs = (
        ("B365", "B365AHH", "B365AHA", "B365CAHH", "B365CAHA", False, VenueKind.BOOKMAKER),
        ("P", "PAHH", "PAHA", "PCAHH", "PCAHA", False, VenueKind.BOOKMAKER),
        ("Max", "MaxAHH", "MaxAHA", "MaxCAHH", "MaxCAHA", True, VenueKind.AGGREGATE),
        ("Avg", "AvgAHH", "AvgAHA", "AvgCAHH", "AvgCAHA", True, VenueKind.AGGREGATE),
        ("BFE", "BFEAHH", "BFEAHA", "BFECAHH", "BFECAHA", True, VenueKind.EXCHANGE),
    )
    for book, o_h, open_a, c_h, c_a, research_only, venue_kind in ah_specs:
        markets.append(
            DeclaredOddsMarket(
                book=book,
                family=MarketFamily.ASIAN_HANDICAP,
                selections=("home", "away"),
                opening_columns=(o_h, open_a),
                closing_columns=(c_h, c_a),
                venue_kind=venue_kind,
                research_only=research_only,
                opening_line_column="AHh",
                closing_line_column="AHCh",
            )
        )
    return tuple(markets)
