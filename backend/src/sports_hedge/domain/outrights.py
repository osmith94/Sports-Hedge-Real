"""Competition/season outright identity types (Outrights Phase 1A / #437).

Sibling of fixture ``CanonicalEvent`` / ``CanonicalMatchRef``. Do not put
home/away/kickoff on these objects. Do not teach ``EventMatcher`` to ignore
kickoff. Prices and liquidity are forbidden on identity.

PAPER / read-only. Observation-only until a later register onboarding RFC.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sports_hedge.domain.market_scope import MarketScope

FORBIDDEN_FIXTURE_FIELDS = frozenset(
    {
        "home_team",
        "away_team",
        "kickoff_utc",
        "kickoff",
        "canonical_match_id",
        "home_canonical",
        "away_canonical",
    }
)
FORBIDDEN_PRICE_FIELDS = frozenset(
    {
        "price",
        "prices",
        "odds",
        "decimal_odds",
        "quote",
        "quotes",
        "best_back",
        "best_lay",
        "best_bid",
        "best_ask",
        "liquidity",
        "size_at_touch",
        "order_book",
        "volume",
    }
)
NAME_ONLY_PARTICIPANT_PREFIXES = ("name:", "label:")


class OutrightMarketFamily(StrEnum):
    COMPETITION_WINNER = "competition_winner"
    TOP_SCORER = "top_scorer"
    TOP_N_FINISH = "top_n_finish"
    CONFERENCE_WINNER = "conference_winner"
    REGULAR_SEASON_WINS = "regular_season_wins"
    RUNNER_UP = "runner_up"
    RELEGATION = "relegation"
    PLAYER_AWARD = "player_award"
    MATCH_RESULT = "match_result"


class ParticipantType(StrEnum):
    TEAM = "TEAM"
    PLAYER = "PLAYER"


class OutrightListingShape(StrEnum):
    MULTI_RUNNER_EXCLUSIVE = "multi_runner_exclusive"
    BINARY_YES_NO = "binary_yes_no"
    EXCHANGE_BACK_LAY = "exchange_back_lay"


class SeasonEquivalenceState(StrEnum):
    """Phase 1A never admits cross-venue outright equivalence."""

    UNKNOWN = "UNKNOWN"
    OBSERVATION_ONLY = "OBSERVATION_ONLY"
    FAIL_CLOSED = "FAIL_CLOSED"
    UNSUPPORTED = "UNSUPPORTED"


class CanonicalCompetitionSeasonRef(BaseModel):
    """Competition/season subject. Fixture fields are forbidden."""

    model_config = ConfigDict(extra="forbid")

    sport: str
    competition_code: str
    season_id: str
    event_scope: MarketScope = MarketScope.COMPETITION_SEASON

    @field_validator("sport", "competition_code", "season_id")
    @classmethod
    def _require_exact_token(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("competition_season_identity_field_required")
        return text

    @field_validator("event_scope")
    @classmethod
    def _require_season_scope(cls, value: MarketScope) -> MarketScope:
        if value is not MarketScope.COMPETITION_SEASON:
            raise ValueError("event_scope_must_be_competition_season")
        return value


class OutrightSettlementFingerprint(BaseModel):
    """Settlement semantics compared after identity. Incomplete fails closed."""

    model_config = ConfigDict(extra="forbid")

    winner_uniqueness: str
    joint_winner_policy: str
    stat_scope: str
    official_resolution_source: str
    exceptional_policy: str
    rule_version: str
    completion_horizon: str

    def deterministic_key(self) -> str:
        return "|".join(
            [
                self.winner_uniqueness.strip(),
                self.joint_winner_policy.strip(),
                self.stat_scope.strip(),
                self.official_resolution_source.strip(),
                self.exceptional_policy.strip(),
                self.rule_version.strip(),
                self.completion_horizon.strip(),
            ]
        )

    def is_complete(self) -> bool:
        values = (
            self.winner_uniqueness,
            self.joint_winner_policy,
            self.stat_scope,
            self.official_resolution_source,
            self.exceptional_policy,
            self.rule_version,
            self.completion_horizon,
        )
        if any(not str(value or "").strip() for value in values):
            return False
        lowered = [str(value).strip().casefold() for value in values]
        return not any(token in {"unknown", "unproven", ""} for token in lowered)


class CanonicalSeasonMarketIdentity(BaseModel):
    """Full canonical season-market identity including participant + fingerprint."""

    model_config = ConfigDict(extra="forbid")

    subject: CanonicalCompetitionSeasonRef
    market_family: OutrightMarketFamily
    participant_type: ParticipantType
    participant_canonical_id: str
    settlement_fingerprint_version: str
    expected_settlement_horizon: str | None = None

    @field_validator("participant_canonical_id", "settlement_fingerprint_version")
    @classmethod
    def _require_identity_token(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("season_market_identity_field_required")
        return text

    @model_validator(mode="after")
    def reject_name_only_participant(self) -> "CanonicalSeasonMarketIdentity":
        token = self.participant_canonical_id.strip().casefold()
        if token.startswith(NAME_ONLY_PARTICIPANT_PREFIXES):
            raise ValueError("name_only_participant_id_forbidden")
        return self


class OutrightNativeListing(BaseModel):
    """Exact provider-native IDs for one venue listing. Never synthesized."""

    model_config = ConfigDict(extra="forbid")

    venue: str
    native_event_id: str
    native_market_id: str
    native_runner_or_contract_id: str
    listing_shape: OutrightListingShape
    polymarket_condition_id: str | None = None
    polymarket_clob_token_ids: tuple[str, ...] = Field(default_factory=tuple)
    polymarket_event_slug: str | None = None
    polymarket_event_ticker: str | None = None
    kalshi_series_ticker: str | None = None
    matchbook_participant_id: str | None = None
