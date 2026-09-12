from __future__ import annotations


class EventIntelligenceMappingError(ValueError):
    """Raised when a fact cannot be attached without guessing identity."""

    def __init__(self, field: str, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.field = field
        self.reason = reason
        self.detail = detail
