"""Stable MLB identity tokens. Not soccer, NFL, or other baseball competitions."""

MLB_SPORT = "baseball"
MLB_COMPETITION = "mlb"
MLB_SETTLEMENT_NOT_EXECUTABLE = "mlb_settlement_equivalence_not_proven"
MLB_LINE_MISMATCH_REASON = "mlb_total_line_mismatch"
MLB_UNSUPPORTED_FAMILY_REASON = "mlb_market_family_not_stage1"
MLB_GAME_IDENTITY_AMBIGUOUS = "mlb_game_identity_ambiguous"
MLB_GAME_IDENTITY_MISMATCH = "mlb_doubleheader_or_start_mismatch"
MLB_NON_MLB_BASEBALL = "rejected_non_mlb_baseball"

# Captured 2026-09-24 public GET /series. Not invented.
MLB_KALSHI_GAME_SERIES = "KXMLBGAME"
MLB_KALSHI_TOTAL_SERIES = "KXMLBTOTAL"
MLB_KALSHI_APPROVED_SERIES = frozenset(
    {
        MLB_KALSHI_GAME_SERIES,
        MLB_KALSHI_TOTAL_SERIES,
    }
)

# Gamma GET /sports sport=mlb series=3 ordering=away. Not KBO/NPB/WBC/NCAA.
POLYMARKET_MLB_SERIES_ID = "3"
POLYMARKET_MLB_SPORT = "mlb"

# Matchbook GET /lookups/sports Baseball id=3; competition meta-tag.
MATCHBOOK_BASEBALL_SPORT_ID = 3
MATCHBOOK_MLB_COMPETITION_TAG_ID = "1494669213760003"

CANONICAL_MLB_GAME_WINNER = "MLB_GAME_WINNER_FT"
CANONICAL_MLB_TOTAL_RUNS = "MLB_TOTAL_RUNS_FT"
