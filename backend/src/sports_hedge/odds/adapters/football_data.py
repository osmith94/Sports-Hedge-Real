from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.facts.catalog import competition_from_label
from sports_hedge.facts.identity import KickoffPrecision
from sports_hedge.odds.models import QuoteType, RawOddsRecord, VenueKind

# Public football-data.co.uk CSVs. Downloaded only when a local path is supplied
# by the caller; this adapter never bypasses access controls.
FOOTBALL_DATA_PUBLIC_FILES = {
    "E0": "https://www.football-data.co.uk/mmz4281/2526/E0.csv",
    "E1": "https://www.football-data.co.uk/mmz4281/2526/E1.csv",
    "SP1": "https://www.football-data.co.uk/mmz4281/2526/SP1.csv",
}

_TZ = {
    "E0": ZoneInfo("Europe/London"),
    "E1": ZoneInfo("Europe/London"),
    "SP1": ZoneInfo("Europe/Madrid"),
}

_BOOKMAKER_1X2 = {
    "B365": ("B365H", "B365D", "B365A", "B365CH", "B365CD", "B365CA"),
    "PS": ("PSH", "PSD", "PSA", "PSCH", "PSCD", "PSCA"),
}

_BOOKMAKER_OU = {
    "B365": ("B365>2.5", "B365<2.5", "B365C>2.5", "B365C<2.5"),
}


class FootballDataCsvAdapter:
    """Parse public football-data.co.uk style CSVs already obtained by the operator.

    Odds in this source are opening/closing bookmaker quotes (quality C). The
    adapter never invents observation timestamps or liquidity.
    """

    source = "football_data"
    required = False

    def __init__(self, csv_text: str, *, retrieved_at: datetime) -> None:
        self._csv_text = csv_text
        self._retrieved_at = retrieved_at

    @classmethod
    def from_path(cls, path: str | Path, *, retrieved_at: datetime) -> FootballDataCsvAdapter:
        return cls(Path(path).read_text(encoding="utf-8"), retrieved_at=retrieved_at)

    def fetch(self) -> Sequence[RawOddsRecord]:
        reader = csv.DictReader(StringIO(self._csv_text))
        records: list[RawOddsRecord] = []
        for index, row in enumerate(reader):
            records.extend(self._row_records(row, index))
        return records

    def _row_records(self, row: dict[str, str], index: int) -> list[RawOddsRecord]:
        div = (row.get("Div") or "").strip()
        spec = competition_from_label(div)
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
        kickoff, precision = _parse_kickoff(row, div)
        home = (row.get("HomeTeam") or "").strip()
        away = (row.get("AwayTeam") or "").strip()
        match_id = f"{div}:{row.get('Date', '')}:{home}:{away}"
        settlement = SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=False,
            penalties_included=False,
            push_possible=False,
        )
        records: list[RawOddsRecord] = []
        # Facts row so coverage has a denominator even when a market is missing.
        records.append(
            RawOddsRecord(
                source=self.source,
                source_market_id=f"{match_id}:facts",
                source_match_id=match_id,
                source_reference=f"{match_id}:facts",
                source_url=FOOTBALL_DATA_PUBLIC_FILES.get(div),
                competition=spec.display_name,
                season=spec.season,
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
                home_goals=_int_or_none(row.get("FTHG")),
                away_goals=_int_or_none(row.get("FTAG")),
                raw_payload={"div": div, "kind": "facts"},
            )
        )
        for book, columns in _BOOKMAKER_1X2.items():
            open_h, open_d, open_a, close_h, close_d, close_a = columns
            records.extend(
                self._triple(
                    row,
                    match_id=match_id,
                    spec_name=spec.display_name,
                    season=spec.season,
                    home=home,
                    away=away,
                    kickoff=kickoff,
                    precision=precision,
                    book=book,
                    family=MarketFamily.MATCH_RESULT,
                    line=None,
                    opening_columns=(open_h, open_d, open_a),
                    closing_columns=(close_h, close_d, close_a),
                    selections=("home", "draw", "away"),
                    settlement=settlement,
                )
            )
        for book, columns in _BOOKMAKER_OU.items():
            open_o, open_u, close_o, close_u = columns
            ou_settlement = SettlementFingerprint(
                scope=SettlementScope.REGULATION_TIME,
                period=FootballPeriod.FULL_TIME,
                line=Decimal("2.5"),
                extra_time_included=False,
                penalties_included=False,
                push_possible=False,
            )
            records.extend(
                self._triple(
                    row,
                    match_id=match_id,
                    spec_name=spec.display_name,
                    season=spec.season,
                    home=home,
                    away=away,
                    kickoff=kickoff,
                    precision=precision,
                    book=book,
                    family=MarketFamily.TOTAL_GOALS,
                    line=Decimal("2.5"),
                    opening_columns=(open_o, open_u),
                    closing_columns=(close_o, close_u),
                    selections=("over", "under"),
                    settlement=ou_settlement,
                )
            )
        return records

    def _triple(
        self,
        row: dict[str, str],
        *,
        match_id: str,
        spec_name: str,
        season: str,
        home: str,
        away: str,
        kickoff: datetime | None,
        precision: KickoffPrecision,
        book: str,
        family: MarketFamily,
        line: Decimal | None,
        opening_columns: tuple[str, ...],
        closing_columns: tuple[str, ...],
        selections: tuple[str, ...],
        settlement: SettlementFingerprint,
    ) -> list[RawOddsRecord]:
        records: list[RawOddsRecord] = []
        for quote_type, columns in (
            (QuoteType.OPENING, opening_columns),
            (QuoteType.CLOSING, closing_columns),
        ):
            for selection, column in zip(selections, columns, strict=True):
                odds = _decimal_or_none(row.get(column))
                if odds is None:
                    continue
                records.append(
                    RawOddsRecord(
                        source=self.source,
                        source_market_id=f"{match_id}:{family.value}:{book}:{quote_type.value}",
                        source_match_id=match_id,
                        source_reference=f"{match_id}:{book}:{column}",
                        source_url=FOOTBALL_DATA_PUBLIC_FILES.get((row.get("Div") or "").strip()),
                        venue=book.casefold(),
                        bookmaker=book.casefold(),
                        venue_kind=VenueKind.BOOKMAKER,
                        competition=spec_name,
                        season=season,
                        home_team=home,
                        away_team=away,
                        kickoff_utc=kickoff,
                        kickoff_precision=precision,
                        market_family=family,
                        period=FootballPeriod.FULL_TIME,
                        line=line,
                        selection=selection,
                        decimal_odds=odds,
                        observed_at=None,
                        quote_type=quote_type,
                        retrieved_at=self._retrieved_at,
                        settlement=settlement,
                        semantics_complete=True,
                        raw_payload={"column": column, "book": book},
                    )
                )
        return records


def _parse_kickoff(row: dict[str, str], div: str) -> tuple[datetime | None, KickoffPrecision]:
    date_raw = (row.get("Date") or "").strip()
    time_raw = (row.get("Time") or "").strip()
    if not date_raw:
        return None, KickoffPrecision.UNKNOWN
    parts = date_raw.split("/")
    if len(parts) != 3:
        return None, KickoffPrecision.UNKNOWN
    try:
        day, month, year = (int(part) for part in parts)
    except ValueError:
        return None, KickoffPrecision.UNKNOWN
    if year < 100:
        year += 2000
    tzinfo = _TZ.get(div, ZoneInfo("UTC"))
    hour = 0
    minute = 0
    precision = KickoffPrecision.DATE
    if time_raw:
        time_parts = time_raw.split(":")
        if len(time_parts) == 2:
            try:
                hour = int(time_parts[0])
                minute = int(time_parts[1])
                precision = KickoffPrecision.MINUTE
            except ValueError:
                precision = KickoffPrecision.DATE
    return datetime(year, month, day, hour, minute, tzinfo=tzinfo), precision


def _decimal_or_none(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    if parsed <= 1:
        return None
    return parsed


def _int_or_none(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None
