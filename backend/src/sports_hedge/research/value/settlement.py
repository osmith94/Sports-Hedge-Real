from __future__ import annotations

from sports_hedge.domain.football import FootballPeriod, SettlementFingerprint, SettlementScope


def settlement_is_complete(fingerprint: SettlementFingerprint) -> bool:
    """Fail closed when economically material settlement rules are missing."""

    if fingerprint.scope == SettlementScope.UNKNOWN:
        return False
    if fingerprint.period == FootballPeriod.UNKNOWN:
        return False
    if fingerprint.extra_time_included is None:
        return False
    if fingerprint.penalties_included is None:
        return False
    return True
