from __future__ import annotations

from sports_hedge.historical.models import CoverageCell, MatchRecord
from sports_hedge.historical.repository import SqliteHistoricalRepository

COVERAGE_FIELDS: tuple[str, ...] = (
    "ft_score",
    "ht_score",
    "goal_timestamps",
    "corners",
    "yellow_cards",
    "red_cards",
    "penalties",
    "substitutions",
    "lineups",
)


class CoverageReporter:
    """Coverage by competition/season/field from the normalized repository."""

    def __init__(self, repository: SqliteHistoricalRepository) -> None:
        self._repository = repository

    def report(
        self,
        *,
        competition_id: str | None = None,
        season_id: str | None = None,
    ) -> list[CoverageCell]:
        matches = self._repository.list_matches(
            competition_id=competition_id,
            season_id=season_id,
        )
        grouped: dict[tuple[str, str], list[MatchRecord]] = {}
        for match in matches:
            grouped.setdefault((match.competition_id, match.season_id), []).append(match)

        cells: list[CoverageCell] = []
        for (_competition_id, _season_id), group in sorted(grouped.items()):
            for field_name in COVERAGE_FIELDS:
                missing = [
                    match.match_id for match in group if not self._field_present(match, field_name)
                ]
                total = len(group)
                present = total - len(missing)
                sample = group[0]
                cells.append(
                    CoverageCell(
                        competition_id=sample.competition_id,
                        competition_name=sample.competition_name,
                        season_id=sample.season_id,
                        season_label=sample.season_label,
                        field_name=field_name,
                        matches_total=total,
                        matches_present=present,
                        coverage_ratio=0.0 if total == 0 else round(present / total, 4),
                        missing_match_ids=missing,
                    )
                )
        return cells

    def _field_present(self, match: MatchRecord, field_name: str) -> bool:
        if field_name == "ft_score":
            return match.home_ft_goals is not None and match.away_ft_goals is not None
        if field_name == "ht_score":
            return match.home_ht_goals is not None and match.away_ht_goals is not None
        if field_name == "goal_timestamps":
            goals = [
                event
                for event in self._repository.list_events(match_id=match.match_id)
                if event.event_type.value == "goal"
            ]
            if match.home_ft_goals is None or match.away_ft_goals is None:
                return False
            expected = match.home_ft_goals + match.away_ft_goals
            if expected == 0:
                return True
            return bool(goals) and all(event.minute is not None for event in goals)
        if field_name in {"corners", "yellow_cards", "red_cards", "penalties"}:
            stats = self._repository.list_team_stats(match_id=match.match_id)
            if len(stats) < 2:
                return False
            return all(getattr(row, field_name) is not None for row in stats)
        if field_name == "substitutions":
            events = self._repository.list_events(match_id=match.match_id)
            return any(event.event_type.value == "substitution" for event in events)
        if field_name == "lineups":
            return bool(self._repository.list_lineups(match_id=match.match_id))
        raise ValueError(f"Unknown coverage field '{field_name}'")

