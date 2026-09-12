from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.facts.catalog import competition_from_label
from sports_hedge.facts.identity import KickoffPrecision, season_for_kickoff
from sports_hedge.odds.models import QuoteType, RawOddsRecord
from sports_hedge.sources.football_data import (
    KICKOFF_TIMEZONES,
    int_or_none,
    parse_kickoff,
    public_csv_url,
    rows_from_csv,
    season_code_from_label,
)
from sports_hedge.sources.football_data_odds import DeclaredOddsMarket, declared_odds_markets


class FootballDataCsvAdapter:
    """Parse public football-data.co.uk style CSVs already obtained by the operator.

    Odds in this source are opening/closing bookmaker quotes (quality C). The
    adapter never invents observation timestamps or liquidity. Column presence is
    header-driven from the declared Football-Data maps.
    """

    source = "football_data"
    required = False

    def __init__(
        self,
        csv_text: str,
        *,
        retrieved_at: datetime,
        season_label: str | None = None,
        source_url: str | None = None,
        div: str | None = None,
    ) -> None:
        self._csv_text = csv_text
        self._retrieved_at = retrieved_at
        self._season_label = season_label
        self._source_url = source_url
        self._div = div
        self._markets = declared_odds_markets()

    @classmethod
    def from_path(cls, path: str | Path, *, retrieved_at: datetime) -> FootballDataCsvAdapter:
        return cls(Path(path).read_text(encoding="utf-8-sig"), retrieved_at=retrieved_at)

    def fetch(self) -> Sequence[RawOddsRecord]:
        records: list[RawOddsRecord] = []
        for index, row in enumerate(rows_from_csv(self._csv_text)):
            records.extend(self._row_records(row, index))
        return records

    def _row_records(self, row: dict[str, str], index: int) -> list[RawOddsRecord]:
        div = self._div or (row.get("Div") or "").strip()
        kickoff, precision = parse_kickoff(row, div)
        season = self._season_label
        if season is None and kickoff is not None:
            season = season_for_kickoff(kickoff)
        spec = competition_from_label(div, season or "2025/26") or competition_from_label(div)
        if spec is None:
            return [
                RawOddsRecord(
                    source=self.source,
                    source_reference=f"row:{index}:div",
                    competition=div or None,
                    home_team=row.get("HomeTeam") or None,
                    away_team=row.get("AwayTeam") or None,
                    retrieved_at=self._retrieved_at,
                    raw_payload=dict(row),
                )
            ]
        resolved_season = season or spec.season
        source_url = self._source_url or public_csv_url(div, season_code_from_label(resolved_season))
        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        match_id = f"{season_code_from_label(resolved_season)}:{div}:{row.get('Date', '')}:{home}:{away}"
        settlement = SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=False,
            penalties_included=False,
            push_possible=False,
        )
        records: list[RawOddsRecord] = [
            RawOddsRecord(
                source=self.source,
                source_market_id=f"{match_id}:facts",
                source_match_id=match_id,
                source_reference=f"{match_id}:facts",
                source_url=source_url,
                competition=spec.display_name,
                season=resolved_season,
                home_team=home,
                away_team=away,
                kickoff_utc=kickoff,
                kickoff_precision=precision,
                market_family=MarketFamily.MATCH_RESULT,
                period=FootballPeriod.FULL_TIME,
                selection="unavailable",
                decimal_odds=None,
                quote_type=QuoteType.UNKNOWN,
                retrieved_at=self._retrieved_at,
                settlement=settlement,
                semantics_complete=True,
                home_goals=int_or_none(row.get("FTHG")),
                away_goals=int_or_none(row.get("FTAG")),
                raw_payload={
                    "div": div,
                    "kind": "facts",
                    "season_code": season_code_from_label(resolved_season),
                    "kickoff_timezone": str(KICKOFF_TIMEZONES.get(div)),
                    "source_url": source_url,
                },
            )
        ]
        present = {key for key, value in row.items() if value}
        for market in self._markets:
            records.extend(
                self._market_records(
                    row,
                    present=present,
                    match_id=match_id,
                    spec_name=spec.display_name,
                    season=resolved_season,
                    source_url=source_url,
                    home=home,
                    away=away,
                    kickoff=kickoff,
                    precision=precision,
                    market=market,
                )
            )
        return records

    def _market_records(
        self,
        row: dict[str, str],
        *,
        present: set[str],
        match_id: str,
        spec_name: str,
        season: str,
        source_url: str,
        home: str,
        away: str,
        kickoff: datetime | None,
        precision: KickoffPrecision,
        market: DeclaredOddsMarket,
    ) -> list[RawOddsRecord]:
        records: list[RawOddsRecord] = []
        for quote_type, columns, line_column in (
            (QuoteType.OPENING, market.opening_columns, market.opening_line_column),
            (QuoteType.CLOSING, market.closing_columns, market.closing_line_column),
        ):
            if not any(column in present for column in columns):
                continue
            line = market.line
            if line_column:
                parsed_line = _decimal_or_none(row.get(line_column), allow_negative=True)
                if parsed_line is not None:
                    line = parsed_line
            settlement = SettlementFingerprint(
                scope=SettlementScope.REGULATION_TIME,
                period=FootballPeriod.FULL_TIME,
                line=line,
                extra_time_included=False,
                penalties_included=False,
                push_possible=market.family == MarketFamily.ASIAN_HANDICAP,
            )
            for selection, column in zip(market.selections, columns, strict=True):
                odds = _decimal_or_none(row.get(column))
                if odds is None:
                    continue
                records.append(
                    RawOddsRecord(
                        source=self.source,
                        source_market_id=(
                            f"{match_id}:{market.family.value}:{market.book}:{quote_type.value}"
                        ),
                        source_match_id=match_id,
                        source_reference=f"{match_id}:{market.book}:{column}",
                        source_url=source_url,
                        venue=market.book.casefold(),
                        bookmaker=market.book.casefold(),
                        venue_kind=market.venue_kind,
                        competition=spec_name,
                        season=season,
                        home_team=home,
                        away_team=away,
                        kickoff_utc=kickoff,
                        kickoff_precision=precision,
                        market_family=market.family,
                        period=FootballPeriod.FULL_TIME,
                        line=line,
                        selection=selection,
                        decimal_odds=odds,
                        observed_at=None,
                        quote_type=quote_type,
                        retrieved_at=self._retrieved_at,
                        settlement=settlement,
                        semantics_complete=True,
                        raw_payload={
                            "column": column,
                            "book": market.book,
                            "research_only": market.research_only,
                            "source_url": source_url,
                        },
                    )
                )
        return records


def _decimal_or_none(value: str | None, *, allow_negative: bool = False) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    if not allow_negative and parsed <= 1:
        return None
    return parsed
