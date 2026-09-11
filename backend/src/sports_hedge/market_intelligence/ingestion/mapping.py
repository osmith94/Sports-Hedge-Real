from __future__ import annotations

from sports_hedge.market_intelligence.ingestion.contracts import (
    MappingIssue,
    NormalizedMarketEvent,
    ProviderEventRecord,
)
from sports_hedge.market_intelligence.models import AnnotationCategory
from sports_hedge.normalization.text import normalize_text


class EventMappingError(ValueError):
    """Raised when a provider event cannot be mapped without guessing."""

    def __init__(self, issue: MappingIssue) -> None:
        super().__init__(issue.detail)
        self.issue = issue


_CATEGORY_ALIASES: dict[str, AnnotationCategory] = {
    "team_sheet": AnnotationCategory.TEAM_SHEET,
    "team sheet": AnnotationCategory.TEAM_SHEET,
    "teamsheet": AnnotationCategory.TEAM_SHEET,
    "starting xi": AnnotationCategory.TEAM_SHEET,
    "startingxi": AnnotationCategory.TEAM_SHEET,
    "lineup": AnnotationCategory.TEAM_SHEET,
    "player out": AnnotationCategory.PLAYER_OUT,
    "playerout": AnnotationCategory.PLAYER_OUT,
    "omitted": AnnotationCategory.PLAYER_OUT,
    "player in": AnnotationCategory.PLAYER_IN,
    "playerin": AnnotationCategory.PLAYER_IN,
    "injury news": AnnotationCategory.INJURY_NEWS,
    "injury": AnnotationCategory.INJURY_NEWS,
    "injurynews": AnnotationCategory.INJURY_NEWS,
    "journalist report": AnnotationCategory.JOURNALIST_REPORT,
    "journalistreport": AnnotationCategory.JOURNALIST_REPORT,
    "news article": AnnotationCategory.NEWS_ARTICLE,
    "newsarticle": AnnotationCategory.NEWS_ARTICLE,
    "goal": AnnotationCategory.GOAL,
    "red card": AnnotationCategory.RED_CARD,
    "redcard": AnnotationCategory.RED_CARD,
    "yellow card": AnnotationCategory.YELLOW_CARD,
    "yellowcard": AnnotationCategory.YELLOW_CARD,
    "penalty": AnnotationCategory.PENALTY,
    "substitution": AnnotationCategory.SUBSTITUTION,
    "sub": AnnotationCategory.SUBSTITUTION,
}

_AMBIGUOUS_CATEGORIES: dict[str, str] = {
    "card": "red_card versus yellow_card",
    "cards": "red_card versus yellow_card",
    "booking": "red_card versus yellow_card",
    "news": "journalist_report versus news_article",
    "report": "journalist_report versus news_article",
    "press": "journalist_report versus news_article",
}


def normalize_provider_event(record: ProviderEventRecord) -> NormalizedMarketEvent:
    category = map_event_category(record.category, record)
    canonical_event_id = _required_canonical_event_id(record)
    title = (record.title or "").strip()
    if not title:
        raise EventMappingError(
            _issue(record, "title", "unmapped", "Event title is required and must not be guessed")
        )
    confidence = 1.0 if record.confidence is None else record.confidence
    return NormalizedMarketEvent(
        provider=record.provider.strip(),
        source_event_id=record.source_event_id.strip(),
        source_occurred_at=record.source_occurred_at,
        retrieved_at=record.retrieved_at,
        category=category,
        canonical_event_id=canonical_event_id,
        title=title,
        confidence=confidence,
        team_ref=_optional_ref(record.team_ref),
        player_ref=_optional_ref(record.player_ref),
        source_url=_optional_ref(record.source_url),
        source_reference=_optional_ref(record.source_reference),
        metadata={
            "ingestion": {
                "provider": record.provider.strip(),
                "source_event_id": record.source_event_id.strip(),
                "source_occurred_at": record.source_occurred_at.isoformat(),
                "retrieved_at": record.retrieved_at.isoformat(),
                "team_ref": _optional_ref(record.team_ref),
                "player_ref": _optional_ref(record.player_ref),
                "home_team": _optional_ref(record.home_team),
                "away_team": _optional_ref(record.away_team),
                "source_reference": _optional_ref(record.source_reference),
                "causal_claim": False,
                "temporal_context_only": True,
            },
            "provider_payload": record.payload,
        },
    )


def map_event_category(raw_category: str, record: ProviderEventRecord) -> AnnotationCategory:
    token = normalize_text(raw_category)
    if not token:
        raise EventMappingError(
            _issue(record, "category", "unmapped", "Event category is required")
        )
    if token in _AMBIGUOUS_CATEGORIES:
        raise EventMappingError(
            _issue(
                record,
                "category",
                "ambiguous",
                (
                    f"Category '{raw_category}' is ambiguous "
                    f"({_AMBIGUOUS_CATEGORIES[token]}); refuse to guess"
                ),
            )
        )
    mapped = _CATEGORY_ALIASES.get(token)
    if mapped is None:
        try:
            return AnnotationCategory(token.replace(" ", "_"))
        except ValueError:
            raise EventMappingError(
                _issue(
                    record,
                    "category",
                    "unmapped",
                    f"Category '{raw_category}' is not a supported market event type",
                )
            ) from None
    return mapped


def _required_canonical_event_id(record: ProviderEventRecord) -> str:
    value = (record.canonical_event_id or "").strip()
    if not value:
        raise EventMappingError(
            _issue(
                record,
                "canonical_event_id",
                "unmapped",
                "Canonical event id is required; team/player labels are not used to guess it",
            )
        )
    return value


def _optional_ref(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _issue(record: ProviderEventRecord, field: str, reason: str, detail: str) -> MappingIssue:
    return MappingIssue(
        field=field,
        reason=reason,
        detail=detail,
        provider=record.provider,
        source_event_id=record.source_event_id,
    )
