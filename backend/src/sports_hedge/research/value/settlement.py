from __future__ import annotations

from sports_hedge.domain.football import SettlementFingerprint


def settlement_is_complete(fingerprint: SettlementFingerprint) -> bool:
    """Fail closed when economically material settlement rules are missing."""

    return fingerprint.is_economically_complete()
