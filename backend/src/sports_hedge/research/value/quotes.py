from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import require_aware_utc
from sports_hedge.research.value.contracts import CanonicalProposition, VenueQuote
from sports_hedge.research.value.economics import quote_cost_rejection, quote_net_odds
from sports_hedge.research.value.settlement import settlement_is_complete

_VENUE_TIEBREAK = {
    VenueName.MATCHBOOK: 0,
    VenueName.SMARKETS: 1,
    VenueName.POLYMARKET: 2,
}


def proposition_matches_quote(proposition: CanonicalProposition, quote: VenueQuote) -> bool:
    return (
        quote.market_family == proposition.market_family
        and quote.period == proposition.period
        and quote.line == proposition.line
        and quote.settlement.deterministic_key() == proposition.settlement.deterministic_key()
        and quote.settlement.deterministic_key() != ""
    )


def semantically_eligible(
    proposition: CanonicalProposition,
    quotes: list[VenueQuote],
) -> tuple[list[VenueQuote], str | None]:
    """Return quotes that are complete and economically equivalent.

    Polymarket is admitted only via the same strict fingerprint match as every
    other venue. Matchbook is not treated as model truth — only as a quote source.
    """

    if not settlement_is_complete(proposition.settlement):
        return [], "incomplete_settlement"

    equivalent: list[VenueQuote] = []
    incomplete_equivalent = False
    for quote in quotes:
        if not proposition_matches_quote(proposition, quote):
            continue
        if not settlement_is_complete(quote.settlement):
            incomplete_equivalent = True
            continue
        equivalent.append(quote)

    if equivalent:
        return equivalent, None
    if incomplete_equivalent:
        return [], "incomplete_settlement"
    return [], "settlement_mismatch"


def quote_timing_rejection(quote: VenueQuote, as_of: datetime) -> str | None:
    require_aware_utc(as_of, "as_of")
    require_aware_utc(quote.quoted_at, "quoted_at")
    if quote.quoted_at > as_of:
        return "future_quote"
    return None


def quote_age_seconds(quote: VenueQuote, as_of: datetime) -> Decimal:
    rejection = quote_timing_rejection(quote, as_of)
    if rejection is not None:
        raise ValueError(rejection)
    age = (as_of - quote.quoted_at).total_seconds()
    return Decimal(str(age))


def is_fresh(quote: VenueQuote, as_of: datetime, max_age_seconds: Decimal) -> bool:
    if quote_timing_rejection(quote, as_of) is not None:
        return False
    return quote_age_seconds(quote, as_of) <= max_age_seconds


def has_sufficient_depth(quote: VenueQuote, min_depth: Decimal) -> bool:
    if quote.available_depth is None:
        return False
    return quote.available_depth >= min_depth


def has_known_costs(quote: VenueQuote, as_of: datetime) -> bool:
    return quote_cost_rejection(quote, as_of) is None


def select_best_quote(quotes: list[VenueQuote]) -> VenueQuote:
    """Best currently available equivalent back/buy quote by net odds.

    Higher net decimal odds are better for the backer. Ties prefer Matchbook,
    then Smarkets, then Polymarket — a display/reference convention, not truth.
    """

    if not quotes:
        raise ValueError("quotes must not be empty")
    return max(
        quotes,
        key=lambda quote: (
            quote_net_odds(quote),
            -_VENUE_TIEBREAK.get(quote.venue, 99),
        ),
    )


def select_reference_quote(
    quotes: list[VenueQuote],
    *,
    reference_venue: VenueName = VenueName.MATCHBOOK,
) -> VenueQuote | None:
    for quote in quotes:
        if quote.venue == reference_venue:
            return quote
    return None
