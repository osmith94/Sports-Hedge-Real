"""Stable NFL identity tokens. Not soccer football."""

NFL_SPORT = "american_football"
NFL_COMPETITION = "NFL"
NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT = "exceptional_settlement_mismatch_possible"
NFL_PAPER_NORMAL_COMPLETION_REASON = "owner_approved_nfl_paper_normal_completion"
# LEGACY audit token. Historical rows may store it. New NFL matches emit
# NFL_REGISTERED_SETTLEMENT_ASSUMPTION instead. This string is not a Real veto.
NFL_NOT_LIVE_EXECUTION_REASON = "nfl_paper_not_live_execution_equivalent"
NFL_REGISTERED_SETTLEMENT_ASSUMPTION = "settlement_assumption=normal_full_game_completion"
NFL_SETTLEMENT_FAIL_CLOSED_REASON = "nfl_exceptional_settlement_fail_closed"
NFL_NORMAL_COMPLETION_NOT_PROVEN = "nfl_normal_completion_not_proven"
NFL_LIFECYCLE_AUDIT_KIND = "nfl_lifecycle"

# Owner-approved Stage 1B registered families only.
NFL_KALSHI_GAME_SERIES = "KXNFLGAME"
NFL_KALSHI_SPREAD_SERIES = "KXNFLSPREAD"
NFL_KALSHI_TOTAL_SERIES = "KXNFLTOTAL"
NFL_KALSHI_APPROVED_SERIES = frozenset(
    {
        NFL_KALSHI_GAME_SERIES,
        NFL_KALSHI_SPREAD_SERIES,
        NFL_KALSHI_TOTAL_SERIES,
    }
)

POLYMARKET_NFL_SERIES_ID = "12185"
POLYMARKET_NFL_SPORT = "nfl"
MATCHBOOK_NFL_COMPETITION_TAG_ID = "491503123380010"

CANONICAL_NFL_GAME_WINNER = "NFL_GAME_WINNER_FT"
CANONICAL_NFL_POINT_SPREAD = "NFL_POINT_SPREAD_FT"
CANONICAL_NFL_TOTAL_POINTS = "NFL_TOTAL_POINTS_FT"
