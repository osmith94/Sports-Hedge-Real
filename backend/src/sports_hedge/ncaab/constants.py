"""Stable NCAA Division I men's basketball identity tokens.

Not soccer, not NFL, not NBA/WNBA, and not NCAAW.
"""

NCAAB_SPORT = "basketball"
NCAAB_COMPETITION = "NCAA Men's Basketball"
NCAAB_EXCEPTIONAL_SETTLEMENT_CAVEAT = "exceptional_settlement_mismatch_possible"
# LEGACY audit token. NCAAB stays blocked because no venue pair is registered.
# New rows emit the unapproved-contract reasons, not this paper-only label.
NCAAB_NOT_LIVE_EXECUTION_REASON = "ncaab_paper_not_live_execution_equivalent"
NCAAB_SETTLEMENT_FAIL_CLOSED_REASON = "ncaab_exceptional_settlement_fail_closed"
NCAAB_NORMAL_COMPLETION_NOT_PROVEN = "ncaab_normal_completion_not_proven"
NCAAB_PAIR_UNAPPROVED_REASON = "ncaab_venue_pair_family_not_evidence_backed"
NCAAB_MISSING_VENUE_EVIDENCE_REASON = "ncaab_missing_participating_venue_evidence"
NCAAB_AUTO_SETTLEMENT_DISABLED_REASON = "ncaab_automatic_settlement_disabled"
NCAAB_LIFECYCLE_AUDIT_KIND = "ncaab_lifecycle"
REJECTED_NON_NCAAB_BASKETBALL = "rejected_non_ncaab_basketball"

# Exact men's game-book series from the 2026-09-22 census. Not the short
# KXNCAAMB stem, which would also cover conference/outright series.
NCAAB_KALSHI_GAME_SERIES = "KXNCAAMBGAME"
NCAAB_KALSHI_SPREAD_SERIES = "KXNCAAMBSPREAD"
NCAAB_KALSHI_TOTAL_SERIES = "KXNCAAMBTOTAL"
NCAAB_KALSHI_APPROVED_SERIES = frozenset(
    {
        NCAAB_KALSHI_GAME_SERIES,
        NCAAB_KALSHI_SPREAD_SERIES,
        NCAAB_KALSHI_TOTAL_SERIES,
    }
)
NCAAB_KALSHI_MENS_PREFIX = "KXNCAAMB"
NCAAB_KALSHI_WOMENS_PREFIX = "KXNCAAWB"
NCAAB_KALSHI_LEGACY_GAME_SERIES = "KXNCAABGAME"

# Polymarket College Basketball game book. Sport code `ncaab` / series 39 is
# March Madness outrights, not the fixture pipeline.
POLYMARKET_NCAAB_SERIES_ID = "10470"
POLYMARKET_NCAAB_SPORT = "cbb"
POLYMARKET_MARCH_MADNESS_SPORT = "ncaab"
POLYMARKET_MARCH_MADNESS_SERIES_ID = "39"
POLYMARKET_NCAAW_SPORT = "cwbb"
POLYMARKET_NCAAW_SERIES_ID = "10471"

MATCHBOOK_BASKETBALL_SPORT_ID = "4"
MATCHBOOK_WNBA_COMPETITION_TAG_ID = "502879947700009"
MATCHBOOK_NBA_COMPETITION_TAG_ID = "406202315670010"

CANONICAL_NCAAB_GAME_WINNER = "NCAAB_GAME_WINNER_FT"
CANONICAL_NCAAB_POINT_SPREAD = "NCAAB_POINT_SPREAD_FT"
CANONICAL_NCAAB_TOTAL_POINTS = "NCAAB_TOTAL_POINTS_FT"

NCAAB_DIVISION_I_PROGRAM_COUNT = 362
