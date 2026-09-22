"""Stable NBA identity tokens. Not soccer football and not NFL."""

NBA_SPORT = "basketball"
NBA_COMPETITION = "NBA"
NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT = "exceptional_settlement_mismatch_possible"
NBA_PAPER_NORMAL_COMPLETION_REASON = "owner_approved_nba_paper_normal_completion"
NBA_NOT_LIVE_EXECUTION_REASON = "nba_paper_not_live_execution_equivalent"
NBA_SETTLEMENT_FAIL_CLOSED_REASON = "nba_exceptional_settlement_fail_closed"
NBA_NORMAL_COMPLETION_NOT_PROVEN = "nba_normal_completion_not_proven"
NBA_LIFECYCLE_AUDIT_KIND = "nba_lifecycle"
NBA_PAIR_UNAPPROVED_REASON = "nba_venue_pair_family_not_evidence_backed"

# Evidence-backed Kalshi match-level series from #453. TEAMTOTAL is captured
# only as a reject series.
NBA_KALSHI_GAME_SERIES = "KXNBAGAME"
NBA_KALSHI_SPREAD_SERIES = "KXNBASPREAD"
NBA_KALSHI_TOTAL_SERIES = "KXNBATOTAL"
NBA_KALSHI_APPROVED_SERIES = frozenset(
    {
        NBA_KALSHI_GAME_SERIES,
        NBA_KALSHI_SPREAD_SERIES,
        NBA_KALSHI_TOTAL_SERIES,
    }
)

POLYMARKET_NBA_SERIES_ID = "10345"
POLYMARKET_NBA_SPORT = "nba"
MATCHBOOK_BASKETBALL_SPORT_ID = "4"
MATCHBOOK_NBA_COMPETITION_TAG_ID = "406202315670010"

CANONICAL_NBA_GAME_WINNER = "NBA_GAME_WINNER_FT"
CANONICAL_NBA_POINT_SPREAD = "NBA_POINT_SPREAD_FT"
CANONICAL_NBA_TOTAL_POINTS = "NBA_TOTAL_POINTS_FT"
