from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    polymarket_gamma_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_base_url: str = "https://clob.polymarket.com"

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

    @model_validator(mode="after")
    def enforce_phase_one_safety(self) -> "Settings":
        if self.sports_hedge_mode != "paper":
            raise ValueError("Phase 1 supports paper mode only")
        if self.sports_hedge_execution_enabled:
            raise ValueError("Live execution is intentionally unavailable in Phase 1")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
