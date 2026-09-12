from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sports_hedge.odds.models import OddsObservation, QuoteType


@dataclass(frozen=True, slots=True)
class OpenCloseMove:
    """Same-proposition opening→closing implied-probability/logit move. Not a prediction."""

    canonical_match_id: str
    competition_code: str
    season: str
    bookmaker: str
    market_family: str
    selection: str
    line: Decimal | None
    opening_odds: Decimal
    closing_odds: Decimal
    implied_probability_delta: Decimal
    implied_logit_delta: float | None
    research_only: bool


@dataclass(frozen=True, slots=True)
class OpenCloseLineShift:
    """Opening and closing quotes whose market line changed. Not a price-move analogue."""

    canonical_match_id: str
    competition_code: str
    season: str
    bookmaker: str
    market_family: str
    selection: str
    opening_line: Decimal | None
    closing_line: Decimal | None
    opening_odds: Decimal
    closing_odds: Decimal
    research_only: bool


@dataclass(frozen=True, slots=True)
class OpenCloseClassification:
    price_moves: tuple[OpenCloseMove, ...]
    line_shifts: tuple[OpenCloseLineShift, ...]


def _identity_key(item: OddsObservation) -> tuple[str, str, str, str]:
    return (
        item.canonical_match_id,
        item.bookmaker or "",
        item.market_family.value,
        item.selection,
    )


def _proposition_key(item: OddsObservation) -> tuple[object, ...]:
    return (*_identity_key(item), item.line)


def classify_open_close(observations: list[OddsObservation]) -> OpenCloseClassification:
    """Split equivalent-proposition price moves from structural line changes."""

    opening: dict[tuple[object, ...], OddsObservation] = {}
    closing: dict[tuple[object, ...], OddsObservation] = {}
    opening_by_identity: dict[tuple[str, str, str, str], OddsObservation] = {}
    closing_by_identity: dict[tuple[str, str, str, str], OddsObservation] = {}
    for item in observations:
        if item.decimal_odds is None:
            continue
        if item.quote_type not in {QuoteType.OPENING, QuoteType.CLOSING}:
            continue
        proposition = _proposition_key(item)
        identity = _identity_key(item)
        if item.quote_type == QuoteType.OPENING:
            opening[proposition] = item
            opening_by_identity[identity] = item
        else:
            closing[proposition] = item
            closing_by_identity[identity] = item

    price_moves: list[OpenCloseMove] = []
    for proposition, opened in opening.items():
        closed = closing.get(proposition)
        if closed is None:
            continue
        move = _price_move(opened, closed)
        if move is not None:
            price_moves.append(move)

    line_shifts: list[OpenCloseLineShift] = []
    for identity, opened in opening_by_identity.items():
        closed = closing_by_identity.get(identity)
        if closed is None or opened.decimal_odds is None or closed.decimal_odds is None:
            continue
        if opened.line == closed.line:
            continue
        line_shifts.append(
            OpenCloseLineShift(
                canonical_match_id=opened.canonical_match_id,
                competition_code=opened.competition_code,
                season=opened.season,
                bookmaker=opened.bookmaker or "",
                market_family=opened.market_family.value,
                selection=opened.selection,
                opening_line=opened.line,
                closing_line=closed.line,
                opening_odds=opened.decimal_odds,
                closing_odds=closed.decimal_odds,
                research_only=bool(opened.metadata.get("research_only")),
            )
        )
    return OpenCloseClassification(
        price_moves=tuple(price_moves),
        line_shifts=tuple(line_shifts),
    )


def pair_open_close(observations: list[OddsObservation]) -> list[OpenCloseMove]:
    """Pair opening/closing quotes only when the market proposition (including line) matches."""

    return list(classify_open_close(observations).price_moves)


def _price_move(opened: OddsObservation, closed: OddsObservation) -> OpenCloseMove | None:
    open_p = opened.implied_probability()
    close_p = closed.implied_probability()
    if open_p is None or close_p is None or opened.decimal_odds is None or closed.decimal_odds is None:
        return None
    open_logit = opened.implied_logit()
    close_logit = closed.implied_logit()
    logit_delta = None
    if open_logit is not None and close_logit is not None:
        logit_delta = close_logit - open_logit
    return OpenCloseMove(
        canonical_match_id=opened.canonical_match_id,
        competition_code=opened.competition_code,
        season=opened.season,
        bookmaker=opened.bookmaker or "",
        market_family=opened.market_family.value,
        selection=opened.selection,
        line=opened.line,
        opening_odds=opened.decimal_odds,
        closing_odds=closed.decimal_odds,
        implied_probability_delta=close_p - open_p,
        implied_logit_delta=logit_delta,
        research_only=bool(opened.metadata.get("research_only")),
    )
