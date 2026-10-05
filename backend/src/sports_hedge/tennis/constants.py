"""Stable tennis identity tokens. Not soccer, not table tennis."""

TENNIS_SPORT = "tennis"
TENNIS_TOUR_ATP = "ATP"
TENNIS_TOUR_WTA = "WTA"
TENNIS_EVENT_SINGLES = "singles"
TENNIS_EVENT_DOUBLES = "doubles"
TENNIS_EVENT_UNKNOWN = "unknown"

# Captured 2026-09-24. Retirement / walkover / postponement rules are not
# equivalent across Kalshi, Polymarket and Matchbook. Owner decision
# 2026-09-26 admits structurally identical singles Match Winner. The
# historical reason string stays on the fingerprint.
TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT = "tennis_retirement_settlement_not_equivalent"
TENNIS_ROUND_UNAVAILABLE = "tennis_round_unavailable"
TENNIS_ROUND_MISMATCH = "tennis_round_mismatch"
TENNIS_TOURNAMENT_MISMATCH = "tennis_tournament_mismatch"
TENNIS_TOURNAMENT_NOT_ADMITTED = "tennis_tournament_not_admitted"
TENNIS_TOUR_MISMATCH = "tennis_tour_mismatch"
TENNIS_EVENT_TYPE_NOT_SINGLES = "tennis_event_type_not_singles"
TENNIS_PLAYER_IDENTITY_AMBIGUOUS = "tennis_player_identity_ambiguous"
TENNIS_PLAYER_IDENTITY_UNRESOLVED = "tennis_player_identity_unresolved"
TENNIS_PLAYER_MISMATCH = "tennis_player_mismatch"
# Supporting clock only. Tour, tournament, round and player pair stay authoritative.
# Candidate generation must use at least this window and must not inherit the
# 5-minute scheduled-team tolerance.
TENNIS_SUPPORTING_KICKOFF_WINDOW_SECONDS = 14 * 24 * 60 * 60
TENNIS_SCHEDULE_OUTSIDE_SUPPORTING_WINDOW = "tennis_schedule_outside_supporting_window"
TENNIS_PARTICIPANT_ORDER_REVERSED = "participant_order_reversed"
TENNIS_SCHEDULE_DRIFT = "schedule_drift_within_supporting_window"

# Public Kalshi GET /series?category=Sports on 2026-09-24.
# Match-level singles only. Doubles, challenger, ITF, sets, games, spreads,
# totals and outrights are observed and not admitted.
TENNIS_KALSHI_ATP_MATCH_SERIES = "KXATPMATCH"
TENNIS_KALSHI_WTA_MATCH_SERIES = "KXWTAMATCH"
TENNIS_KALSHI_APPROVED_SERIES = frozenset(
    {
        TENNIS_KALSHI_ATP_MATCH_SERIES,
        TENNIS_KALSHI_WTA_MATCH_SERIES,
    }
)

# Public Gamma GET /sports on 2026-09-24.
POLYMARKET_ATP_SERIES_ID = "10365"
POLYMARKET_WTA_SERIES_ID = "10366"
POLYMARKET_ATP_DOUBLES_SERIES_ID = "11632"
POLYMARKET_WTA_DOUBLES_SERIES_ID = "11633"
POLYMARKET_ITF_SERIES_ID = "11634"
POLYMARKET_ATP_SPORT = "atp"
POLYMARKET_WTA_SPORT = "wta"

# Public Matchbook GET /edge/rest/lookups/sports on 2026-09-24. Table Tennis is
# a different sport id and is not tennis.
MATCHBOOK_TENNIS_SPORT_ID = "9"

CANONICAL_TENNIS_MATCH_WINNER = "TENNIS_MATCH_WINNER"
