"""Shared lifecycle decision / audit primitives.

Illegal transitions are rejected with a stable reason code. Audit records are
append-only: callers may add events, never rewrite or delete prior ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class LifecycleDecision:
    """Result of one attempted state transition.

    ``reason`` is the auditable code. Allowed transitions use a short action
    name; rejected transitions use a stable ``illegal_*`` / domain error code
    that production wrap points can raise or no-op with.
    """

    accepted: bool
    from_state: str
    to_state: str
    action: str
    reason: str
    machine: str
    detail: str | None = None
    generation_id: int | None = None
    chunk_epoch: int | None = None
    occurred_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_audit_dict(self) -> dict[str, Any]:
        payload = {
            "accepted": self.accepted,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "action": self.action,
            "reason": self.reason,
            "machine": self.machine,
            "detail": self.detail,
            "generation_id": self.generation_id,
            "chunk_epoch": self.chunk_epoch,
        }
        if self.occurred_at is not None:
            payload["occurred_at"] = self.occurred_at.isoformat()
        if self.extra:
            payload["extra"] = dict(self.extra)
        return payload


class IllegalLifecycleTransition(ValueError):
    """Raised when a caller requires a legal transition and the graph rejects it."""

    def __init__(self, decision: LifecycleDecision):
        super().__init__(decision.reason)
        self.decision = decision


class LifecycleAuditLog:
    """Append-only in-process lifecycle audit.

    ``events`` returns a tuple snapshot. There is no rewrite, pop, or clear
    API; a RESET/CLEAR action is itself another appended event.
    """

    def __init__(self) -> None:
        self._events: list[LifecycleDecision] = []

    def append(self, decision: LifecycleDecision) -> LifecycleDecision:
        self._events.append(decision)
        return decision

    @property
    def events(self) -> tuple[LifecycleDecision, ...]:
        return tuple(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def reasons(self) -> tuple[str, ...]:
        return tuple(event.reason for event in self._events)

    def rejected(self) -> tuple[LifecycleDecision, ...]:
        return tuple(event for event in self._events if not event.accepted)


def allowed_decision(
    *,
    from_state: str,
    to_state: str,
    action: str,
    machine: str,
    reason: str | None = None,
    **kwargs: Any,
) -> LifecycleDecision:
    return LifecycleDecision(
        accepted=True,
        from_state=str(from_state),
        to_state=str(to_state),
        action=action,
        reason=reason or action,
        machine=machine,
        occurred_at=kwargs.pop("occurred_at", datetime.now(UTC)),
        **kwargs,
    )


def rejected_decision(
    *,
    from_state: str,
    to_state: str,
    action: str,
    machine: str,
    reason: str,
    **kwargs: Any,
) -> LifecycleDecision:
    return LifecycleDecision(
        accepted=False,
        from_state=str(from_state),
        to_state=str(to_state),
        action=action,
        reason=reason,
        machine=machine,
        occurred_at=kwargs.pop("occurred_at", datetime.now(UTC)),
        **kwargs,
    )


def require_accepted(decision: LifecycleDecision) -> LifecycleDecision:
    if not decision.accepted:
        raise IllegalLifecycleTransition(decision)
    return decision
