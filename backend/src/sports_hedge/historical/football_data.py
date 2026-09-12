from __future__ import annotations

from datetime import datetime

from sports_hedge.historical.models import MatchStatus, SourceMatchPayload
from sports_hedge.sources.football_data import (
    DIVISION_COMPETITION_ID,
    KICKOFF_TIMEZONES,
    int_or_none,
    parse_kickoff,
    public_csv_url,
    rows_from_csv,
    season_code_from_label,
)


class FootballDataHistoricalAdapter:
    """Map public Football-Data.co.uk match rows to facts payloads.

    The source does not supply goal minutes, substitutions or lineups.
    Those fields stay empty rather than being invented.
    """

    name = "football_data"

    def __init__(
        self,
        csv_text: str,
        *,
        retrieved_at: datetime,
        source_url: str | None = None,
        div: str,
        season_label: str,
    ) -> None:
        self._csv_text = csv_text
        self._retrieved_at = retrieved_at
        self._div = div
        self._season_label = season_label
        self._season_code = season_code_from_label(season_label)
        self._source_url = source_url or public_csv_url(div, self._season_code)

    def fetch_matches(self, competition_id: str, season_label: str) -> list[SourceMatchPayload]:
        expected = DIVISION_COMPETITION_ID[self._div]
        if competition_id != expected:
            return []
        payloads: list[SourceMatchPayload] = []
        for index, row in enumerate(rows_from_csv(self._csv_text)):
            if (row.get("Div") or "").strip() != self._div:
                continue
            payloads.append(self._payload(row, index))
        return payloads

    def _payload(self, row: dict[str, str], index: int) -> SourceMatchPayload:
        home = row.get("HomeTeam") or ""
        away = row.get("AwayTeam") or ""
        kickoff, _precision = parse_kickoff(row, self._div)
        if kickoff is None:
            raise ValueError(f"Football-Data {self._div} row {index} is missing a usable kickoff")
        source_match_id = f"{self._season_code}:{self._div}:{row.get('Date', '')}:{home}:{away}"
        competition_name = "Premier League" if self._div == "E0" else "Championship"
        return SourceMatchPayload(
            source_name=self.name,
            source_match_id=source_match_id,
            source_url=self._source_url,
            retrieved_at=self._retrieved_at,
            source_timestamp=None,
            competition_name=competition_name,
            season_label=self._season_label,
            kickoff_utc=kickoff,
            home_team=home,
            away_team=away,
            status=MatchStatus.FINISHED
            if int_or_none(row.get("FTHG")) is not None
            else MatchStatus.SCHEDULED,
            home_ft_goals=int_or_none(row.get("FTHG")),
            away_ft_goals=int_or_none(row.get("FTAG")),
            home_ht_goals=int_or_none(row.get("HTHG")),
            away_ht_goals=int_or_none(row.get("HTAG")),
            venue_name=None,
            home_corners=int_or_none(row.get("HC")),
            away_corners=int_or_none(row.get("AC")),
            home_yellow_cards=int_or_none(row.get("HY")),
            away_yellow_cards=int_or_none(row.get("AY")),
            home_red_cards=int_or_none(row.get("HR")),
            away_red_cards=int_or_none(row.get("AR")),
            home_penalties=None,
            away_penalties=None,
            events=[],
            lineups=[],
            raw_payload={
                "div": self._div,
                "date": row.get("Date"),
                "time": row.get("Time"),
                "home": home,
                "away": away,
                "fthg": row.get("FTHG"),
                "ftag": row.get("FTAG"),
                "hthg": row.get("HTHG"),
                "htag": row.get("HTAG"),
                "hc": row.get("HC"),
                "ac": row.get("AC"),
                "hy": row.get("HY"),
                "ay": row.get("AY"),
                "hr": row.get("HR"),
                "ar": row.get("AR"),
                "kickoff_timezone": str(KICKOFF_TIMEZONES[self._div]),
                "season_code": self._season_code,
                "source_url": self._source_url,
                "row_index": index,
            },
        )
