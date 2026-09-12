from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from sports_hedge.odds.timestamps import require_aware

QuoteAgeBasis = Literal["source", "retrieval", "unknown"]


class QuoteAgeError(ValueError):
    """Raised when a required quote clock cannot be used honestly."""


class QuoteAgeAssessment(BaseModel):
    quote_age_ms: int | None = Field(default=None, ge=0)
    basis: QuoteAgeBasis
    reason: str | None = None


def require_aware_instant(value: datetime, field: str) -> datetime:
    """Reject naive instants; convert aware non-UTC offsets to UTC."""

    return require_aware(value, field).astimezone(UTC)


def conservative_combined_age_ms(left: int | None, right: int | None) -> int | None:
    """Unknown stays unknown. Known ages use the older (larger) value."""

    if left is None or right is None:
        return None
    return max(left, right)


def conservative_combined_basis(
    left: QuoteAgeBasis | str | None,
    right: QuoteAgeBasis | str | None,
) -> QuoteAgeBasis:
    """Unknown stays unknown. Retrieval outranks source for the pair."""

    if left not in {"source", "retrieval"} or right not in {"source", "retrieval"}:
        return "unknown"
    if left == "retrieval" or right == "retrieval":
        return "retrieval"
    return "source"


def retrieval_quote_age(
    *,
    retrieved_at: datetime,
    evaluated_at: datetime,
) -> QuoteAgeAssessment:
    retrieved = require_aware_instant(retrieved_at, "retrieved_at")
    evaluated = require_aware_instant(evaluated_at, "evaluated_at")
    age_ms = _age_ms(retrieved, evaluated, source_field="retrieved_at")
    if age_ms is None:
        return QuoteAgeAssessment(
            quote_age_ms=None,
            basis="unknown",
            reason="future_quote_timestamp",
        )
    return QuoteAgeAssessment(quote_age_ms=age_ms, basis="retrieval")


def source_quote_age(
    source_times: list[datetime],
    *,
    evaluated_at: datetime,
    required: int,
) -> QuoteAgeAssessment:
    """Age from the oldest required source timestamp. Missing/invalid/future fail closed."""

    evaluated = require_aware_instant(evaluated_at, "evaluated_at")
    if required <= 0:
        raise QuoteAgeError("required quote count must be positive")
    if len(source_times) < required:
        return QuoteAgeAssessment(
            quote_age_ms=None,
            basis="unknown",
            reason="missing_quote_timestamp",
        )
    oldest: datetime | None = None
    for raw in source_times:
        instant = require_aware_instant(raw, "quote_timestamp")
        if _age_ms(instant, evaluated, source_field="quote_timestamp") is None:
            return QuoteAgeAssessment(
                quote_age_ms=None,
                basis="unknown",
                reason="future_quote_timestamp",
            )
        if oldest is None or instant < oldest:
            oldest = instant
    assert oldest is not None
    age_ms = _age_ms(oldest, evaluated, source_field="quote_timestamp")
    if age_ms is None:
        return QuoteAgeAssessment(
            quote_age_ms=None,
            basis="unknown",
            reason="future_quote_timestamp",
        )
    return QuoteAgeAssessment(quote_age_ms=age_ms, basis="source")


def polymarket_books_quote_age(
    books_by_token: dict[str, dict[str, Any]],
    *,
    required_tokens: list[str],
    evaluated_at: datetime,
) -> QuoteAgeAssessment:
    if not required_tokens:
        raise QuoteAgeError("polymarket quote age requires at least one token")
    source_times: list[datetime] = []
    for token in required_tokens:
        book = books_by_token.get(token)
        if not isinstance(book, dict):
            return QuoteAgeAssessment(
                quote_age_ms=None,
                basis="unknown",
                reason="missing_quote_timestamp",
            )
        try:
            source_times.append(parse_quote_clock(book.get("timestamp"), field="timestamp"))
        except QuoteAgeError as exc:
            reason = str(exc)
            if "naive" in reason:
                fail = "naive_quote_timestamp"
            elif "invalid" in reason:
                fail = "invalid_quote_timestamp"
            else:
                fail = "missing_quote_timestamp"
            return QuoteAgeAssessment(quote_age_ms=None, basis="unknown", reason=fail)
    return source_quote_age(source_times, evaluated_at=evaluated_at, required=len(required_tokens))


def matchbook_market_quote_age(
    market_payload: dict[str, Any],
    *,
    retrieved_at: datetime,
    evaluated_at: datetime,
) -> QuoteAgeAssessment:
    """Use Matchbook source clocks only when every priced runner/level has one.

    Missing clocks may fall back to retrieval age, but only after already-collected
    clocks are validated. Known invalid/future clocks must not be masked.
    """

    runners = [
        runner
        for runner in market_payload.get("runners", []) or []
        if isinstance(runner, dict) and runner.get("prices")
    ]
    source_times: list[datetime] = []
    missing_required = False
    for runner in runners:
        stamps, runner_missing = _matchbook_runner_clocks(runner)
        missing_required = missing_required or runner_missing
        for stamp in stamps:
            try:
                source_times.append(parse_quote_clock(stamp, field="matchbook_quote_timestamp"))
            except QuoteAgeError as exc:
                message = str(exc)
                reason = "naive_quote_timestamp" if "naive" in message else "invalid_quote_timestamp"
                return QuoteAgeAssessment(quote_age_ms=None, basis="unknown", reason=reason)
    if source_times:
        assessed = source_quote_age(
            source_times,
            evaluated_at=evaluated_at,
            required=len(source_times),
        )
        if assessed.reason is not None:
            return assessed
        if missing_required:
            return retrieval_quote_age(retrieved_at=retrieved_at, evaluated_at=evaluated_at)
        return assessed
    return retrieval_quote_age(retrieved_at=retrieved_at, evaluated_at=evaluated_at)


def parse_quote_clock(value: Any, *, field: str) -> datetime:
    try:
        return _parse_quote_clock(value, field=field)
    except QuoteAgeError:
        raise
    except (OverflowError, OSError, ValueError) as exc:
        raise QuoteAgeError(f"invalid {field}") from exc


def _parse_quote_clock(value: Any, *, field: str) -> datetime:
    if value is None or value == "":
        raise QuoteAgeError(f"missing {field}")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise QuoteAgeError(f"naive {field}")
        return value.astimezone(UTC)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise QuoteAgeError(f"invalid {field}")
    if isinstance(value, str) and value.strip().casefold() in {
        "nan",
        "inf",
        "+inf",
        "-inf",
        "infinity",
        "+infinity",
        "-infinity",
    }:
        raise QuoteAgeError(f"invalid {field}")
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        try:
            numeric = int(str(value).strip())
        except (ValueError, OverflowError) as exc:
            raise QuoteAgeError(f"invalid {field}") from exc
        if numeric < 10_000_000_000:
            numeric *= 1000
        if numeric < 0:
            raise QuoteAgeError(f"invalid {field}")
        try:
            return datetime.fromtimestamp(numeric / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise QuoteAgeError(f"invalid {field}") from exc
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise QuoteAgeError(f"invalid {field}") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise QuoteAgeError(f"naive {field}")
    return parsed.astimezone(UTC)


def effective_quote_age_ms(
    stored_quote_age_ms: int | None,
    last_seen_at: datetime,
    as_of: datetime,
) -> int | None:
    """Age a persisted observation by wall-clock time since last collection.

    Does not mutate the stored age. Unknown stays unknown.
    """

    if stored_quote_age_ms is None:
        return None
    last_seen = require_aware_instant(last_seen_at, "last_seen_at")
    evaluated = require_aware_instant(as_of, "as_of")
    elapsed_ms = (evaluated - last_seen).total_seconds() * 1000
    if elapsed_ms < 0:
        return None
    return stored_quote_age_ms + int(elapsed_ms)


def _age_ms(source: datetime, evaluated: datetime, *, source_field: str) -> int | None:
    delta_ms = (evaluated - source).total_seconds() * 1000
    if delta_ms < 0:
        return None
    return int(delta_ms)


def _first_scalar(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None and payload[key] != "":
            return payload[key]
    return None


_CLOCK_KEYS = ("timestamp", "last-updated", "last_updated", "last-updated-at")


def _matchbook_runner_clocks(runner: dict[str, Any]) -> tuple[list[Any], bool]:
    """Collect every priced-level clock. Missing required clocks are reported separately."""

    stamps: list[Any] = []
    runner_stamp = _first_scalar(runner, *_CLOCK_KEYS)
    if runner_stamp is not None:
        stamps.append(runner_stamp)
    prices = [price for price in runner.get("prices", []) or [] if isinstance(price, dict)]
    missing_level = False
    for price in prices:
        stamp = _first_scalar(price, *_CLOCK_KEYS)
        if stamp is None:
            missing_level = True
            continue
        stamps.append(stamp)
    missing_required = runner_stamp is None and (not prices or missing_level)
    return stamps, missing_required
