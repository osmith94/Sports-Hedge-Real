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


def settlement_key_without_line(key: str | None) -> str | None:
    """Drop the line field from SettlementFingerprint.deterministic_key."""

    if key is None or key == "":
        return None
    parts = key.split("|")
    if len(parts) < 3:
        return key
    parts[2] = ""
    return "|".join(parts)


def revision_sort_key(item: OddsObservation) -> tuple:
    """Deterministic correction ranking. Independent of input list order."""

    return (item.retrieved_at, item.observation_id)


def revision_identity_key(item: OddsObservation) -> tuple[str, ...]:
    """Stable source observation identity. Line is a revisable property, not the key.

    Distinct concurrently offered lines keep separate source_market_id /
    source_reference values and therefore stay separate.
    """

    return (
        item.source,
        item.source_market_id or "",
        item.source_reference or "",
        item.quote_type.value,
        item.selection,
        "" if item.side is None else item.side.value,
        "na" if item.observed_at is None else item.observed_at.isoformat(),
    )


def _provenance_key(item: OddsObservation) -> tuple[str, ...]:
    return (
        item.selection,
        item.source,
        item.bookmaker or "",
        item.venue or "",
        "" if item.side is None else item.side.value,
    )


def open_close_pair_key(item: OddsObservation) -> tuple[str, ...] | None:
    """Same-line price-move grouping. Incomplete settlement cannot pair."""

    equivalent = item.market_equivalence_key()
    if equivalent is None:
        return None
    return (*equivalent, *_provenance_key(item))


def open_close_identity_key(item: OddsObservation) -> tuple[str, ...] | None:
    """Line-agnostic identity for structural AH (and other) line shifts."""

    if not item.semantics_complete or not item.settlement_key:
        return None
    without_line = settlement_key_without_line(item.settlement_key)
    if without_line is None:
        return None
    return (
        item.canonical_match_id,
        item.market_family.value,
        item.period.value,
        without_line,
        *_provenance_key(item),
    )


def _keep_latest(
    existing: OddsObservation | None, candidate: OddsObservation
) -> OddsObservation:
    if existing is None or revision_sort_key(candidate) > revision_sort_key(existing):
        return candidate
    return existing


def current_revisions(observations: list[OddsObservation]) -> list[OddsObservation]:
    """Keep the latest append-only row per stable source observation identity.

    Ranking includes later corrections with no usable price or incomplete
    semantics so they suppress the prior revision before pairing.
    """

    latest: dict[tuple[str, ...], OddsObservation] = {}
    for item in observations:
        if item.quote_type not in {QuoteType.OPENING, QuoteType.CLOSING}:
            continue
        key = revision_identity_key(item)
        latest[key] = _keep_latest(latest.get(key), item)
    return list(latest.values())


def classify_open_close(observations: list[OddsObservation]) -> OpenCloseClassification:
    """Split equivalent-proposition price moves from structural line changes."""

    opening: dict[tuple[str, ...], OddsObservation] = {}
    closing: dict[tuple[str, ...], OddsObservation] = {}
    opening_by_identity: dict[tuple[str, ...], OddsObservation] = {}
    closing_by_identity: dict[tuple[str, ...], OddsObservation] = {}
    for item in current_revisions(observations):
        if item.decimal_odds is None:
            continue
        if item.quote_type not in {QuoteType.OPENING, QuoteType.CLOSING}:
            continue
        pair_key = open_close_pair_key(item)
        identity = open_close_identity_key(item)
        if item.quote_type == QuoteType.OPENING:
            if pair_key is not None:
                opening[pair_key] = _keep_latest(opening.get(pair_key), item)
            if identity is not None:
                opening_by_identity[identity] = _keep_latest(
                    opening_by_identity.get(identity), item
                )
        else:
            if pair_key is not None:
                closing[pair_key] = _keep_latest(closing.get(pair_key), item)
            if identity is not None:
                closing_by_identity[identity] = _keep_latest(
                    closing_by_identity.get(identity), item
                )

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
