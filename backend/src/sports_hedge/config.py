from __future__ import annotations

from functools import lru_cache
from json import JSONDecodeError, loads
from logging import getLogger
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LOGGER = getLogger(__name__)


class Settings(BaseSettings):
    """Runtime configuration for the Phase 1 paper-only service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    sports_hedge_mode: Literal["paper"] = "paper"
    sports_hedge_execution_enabled: bool = False

    matchbook_username: str | None = None
    matchbook_password: str | None = None
    matchbook_mfa_code: str | None = None
    matchbook_base_url: str = "https://api.matchbook.com"
    matchbook_currency: Literal["GBP", "USD", "EUR", "AUD", "CAD", "HKD"] = "GBP"
    matchbook_price_depth: int = Field(default=5, ge=1, le=50)
    matchbook_minimum_liquidity: float = Field(default=2.0, ge=0)
    # Provider-side event paging. sport-ids is resolved via GET /edge/rest/lookups/sports.
    matchbook_event_per_page: int = Field(default=100, ge=1, le=100)
    matchbook_event_max_pages: int = Field(default=10, ge=1, le=50)
    matchbook_fixture_lookback_hours: int = Field(default=6, ge=1, le=24)
    matchbook_fixture_lookahead_hours: int = Field(default=72, ge=1, le=168)

    polymarket_gamma_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_base_url: str = "https://clob.polymarket.com"
    # Public Gamma GET /sports (2026-09-12): epl=10188, elc=10355, lal=10193.
    # Empty single-id override disables series filtering.
    polymarket_gamma_series_id: str | None = None
    polymarket_gamma_series_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["10188", "10355", "10193"]
    )
    polymarket_gamma_page_limit: int = Field(default=100, ge=1, le=100)
    polymarket_gamma_max_pages_per_series: int = Field(default=5, ge=1, le=20)

    market_intelligence_db_path: str = "./data/market_intelligence.sqlite"
    market_intelligence_minimum_sample_size: int = Field(default=8, ge=2)
    paper_audit_db_path: str = "./data/paper_audit.sqlite"
    paper_liquidity_db_path: str = "./data/paper_liquidity.sqlite"
    watchlist_db_path: str = "./data/near_arb_watchlist.sqlite"
    paper_live_refresh_enabled: bool = False
    paper_live_refresh_interval_seconds: int = Field(default=30, ge=15, le=300)
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )
    notifications_db_path: str = "./data/notifications.sqlite"
    notification_cooldown_seconds: int = Field(default=900, ge=0)
    historical_db_path: str = "./data/historical_football.sqlite"
    historical_odds_db_path: str = "./data/historical_odds.sqlite"
    fx_db_path: str = "./data/fx_rates.sqlite"
    fx_check_tolerance_bps: int = Field(default=25, ge=0)
    fx_stale_after_days: int = Field(default=7, ge=1)
    accounting_schedule_enabled: bool = False

    paper_bankroll_gbp: float = Field(default=5000.0, gt=0)
    paper_bankroll_usd: float = Field(default=5000.0, gt=0)
    min_net_edge: float = Field(default=0.005, ge=0)
    max_slippage_bps: int = Field(default=25, ge=0)
    max_event_exposure_gbp: float = Field(default=1000.0, gt=0)
    max_total_exposure_gbp: float = Field(default=5000.0, gt=0)
    max_execution_risk: int = Field(default=60, ge=0, le=100)
    min_mapping_confidence: float = Field(default=0.98, ge=0, le=1)
    simulated_latency_ms: int = Field(default=500, ge=0)
    simulate_partial_fills: bool = True
    fx_spread_bps: int = Field(default=10, ge=0)

    priority_min_net_edge: float = Field(default=0.03, ge=0)
    priority_min_expected_profit: float = Field(default=20.0, ge=0)
    priority_min_executable_depth: float = Field(default=200.0, ge=0)
    priority_max_quote_age_ms: int = Field(default=2000, ge=0)
    priority_max_execution_risk: int = Field(default=40, ge=0, le=100)
    priority_min_depth_coverage: float = Field(default=1.0, ge=0)
    priority_min_capital_efficiency: float = Field(default=0.03, ge=0)
    priority_safety_haircut: float = Field(default=0.05, ge=0, lt=1)
    priority_operator_manual_cap: float = Field(default=10000.0, gt=0)
    priority_risk_limit: float = Field(default=10000.0, gt=0)

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def split_cors_allow_origins(cls, value: Any) -> list[str]:
        return parse_cors_allow_origins(value)

    @field_validator("polymarket_gamma_series_ids", mode="before")
    @classmethod
    def split_polymarket_series_ids(cls, value: Any) -> list[str]:
        return parse_series_ids(value)

    def resolved_polymarket_series_ids(self) -> list[str]:
        """Single-id override wins when set, including explicit disable (empty)."""

        if self.polymarket_gamma_series_id is not None:
            single = self.polymarket_gamma_series_id.strip()
            return [single] if single else []
        return list(self.polymarket_gamma_series_ids)

    @model_validator(mode="after")
    def enforce_phase_one_safety(self) -> "Settings":
        if self.sports_hedge_mode != "paper":
            raise ValueError("Phase 1 supports paper mode only")
        if self.sports_hedge_execution_enabled:
            raise ValueError("Live execution is intentionally unavailable in Phase 1")
        if self.polymarket_gamma_series_id is not None:
            displayed = self.polymarket_gamma_series_id.strip() or "(empty — series filter disabled)"
            LOGGER.warning(
                "POLYMARKET_GAMMA_SERIES_ID is a legacy single-series override (%s); "
                "Championship and La Liga will not be queried unless this is unset. "
                "Prefer POLYMARKET_GAMMA_SERIES_IDS.",
                displayed,
            )
        return self


def parse_cors_allow_origins(value: Any) -> list[str]:
    """Restrictive console origin allowlist. Wildcard * is rejected."""

    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = loads(text)
            except JSONDecodeError as exc:
                raise ValueError("CORS_ALLOW_ORIGINS JSON list is invalid") from exc
            return parse_cors_allow_origins(parsed)
        origins = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        origins = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ValueError("CORS_ALLOW_ORIGINS must be a list or comma-separated string")
    if any(origin == "*" for origin in origins):
        raise ValueError("CORS allowlist must not include an unrestricted wildcard")
    return origins


def parse_series_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = loads(text)
            except JSONDecodeError as exc:
                raise ValueError("POLYMARKET_GAMMA_SERIES_IDS JSON list is invalid") from exc
            return parse_series_ids(parsed)
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("POLYMARKET_GAMMA_SERIES_IDS must be a list or comma-separated string")


@lru_cache
def get_settings() -> Settings:
    return Settings()
