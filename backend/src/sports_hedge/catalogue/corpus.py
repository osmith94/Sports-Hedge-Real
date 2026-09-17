"""Known-good / known-bad fixture-market corpus for catalogue census v1.

Deterministic fixture/demo payloads only. Not owner-live evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.catalogue.classify import PayloadSide
from sports_hedge.catalogue.states import CatalogueApprovalState, CatalogueArchetype
from sports_hedge.domain.models import VenueName
from sports_hedge.normalization.kalshi_contract_terms import GAMEWIN_SCOPE_UNAVAILABLE_REASON

CENSUS_KICKOFF = datetime(2026, 9, 20, 18, 45, tzinfo=UTC)
REGULATION = "Resolves on 90 minutes of regulation time. Extra time and penalties do not count."
PM_REGULATION = "Resolves based on 90 minutes of regulation time."
ET_RULES = "Resolves including extra time."
GAMEWIN_TEMPLATE = (
    "Series contract terms define result scope that may take one of the "
    "following: first half, regulation time, second half, extra time, or "
    "full match."
)
GAMEWIN_URL = "https://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf"

MB_EVENT: dict[str, Any] = {
    "id": 26801,
    "name": "Tottenham vs Everton",
    "start": CENSUS_KICKOFF.isoformat(),
    "competition-name": "Premier League",
}
PM_EVENT: dict[str, Any] = {
    "id": "pm-census-268",
    "title": "Tottenham vs Everton",
    "startTime": CENSUS_KICKOFF.isoformat(),
    "competition": "Premier League",
}
KALSHI_EVENT: dict[str, Any] = {
    "event_ticker": "KXEPLGAME-26SEP20TOTEVE",
    "series_ticker": "KXEPLGAME",
    "title": "Tottenham vs Everton",
    "category": "Sports",
    "strike_date": CENSUS_KICKOFF.isoformat(),
}
KALSHI_SERIES: dict[str, Any] = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
}
KALSHI_GAMEWIN_SERIES: dict[str, Any] = {
    **KALSHI_SERIES,
    "contract_terms_url": GAMEWIN_URL,
    "contract_family": {
        "family_id": "soccergamewin",
        "defines_default_result_scope": False,
        "default_applies_to_match_result": False,
        "match_result_default_scope": "none",
        "placeholder_specified_by_exchange": True,
        "unknown_reason": GAMEWIN_SCOPE_UNAVAILABLE_REASON,
    },
}


class CorpusEntry(BaseModel):
    entry_id: str
    archetype: CatalogueArchetype
    expected_state: CatalogueApprovalState
    known_kind: str
    left: PayloadSide
    right: PayloadSide
    notes: list[str] = Field(default_factory=list)
    data_class: str = "deterministic_fixture"


def _mb(markets: list[dict[str, Any]]) -> PayloadSide:
    return PayloadSide(venue=VenueName.MATCHBOOK, event=MB_EVENT, markets=markets)


def _pm(markets: list[dict[str, Any]]) -> PayloadSide:
    return PayloadSide(venue=VenueName.POLYMARKET, event=PM_EVENT, markets=markets)


def _kalshi(
    markets: list[dict[str, Any]],
    *,
    series: dict[str, Any] | None = None,
) -> PayloadSide:
    return PayloadSide(
        venue=VenueName.KALSHI,
        event=KALSHI_EVENT,
        markets=markets,
        series=series or KALSHI_SERIES,
    )


def _mb_1x2() -> dict[str, Any]:
    return {
        "id": 26810,
        "name": "Match Odds",
        "runners": [
            {"id": 1, "name": "Tottenham"},
            {"id": 2, "name": "Draw"},
            {"id": 3, "name": "Everton"},
        ],
    }


def _pm_1x2(
    *,
    description: str = PM_REGULATION,
    question: str = "Match result?",
    binary: bool = False,
) -> dict[str, Any]:
    if binary:
        return {
            "id": "pm-1x2-binary",
            "question": question,
            "sportsMarketType": "moneyline",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["y", "n"]',
            "description": description,
        }
    return {
        "id": "pm-1x2",
        "question": question,
        "sportsMarketType": "moneyline",
        "outcomes": '["Tottenham", "Draw", "Everton"]',
        "clobTokenIds": '["h", "d", "a"]',
        "description": description,
    }


def _kalshi_1x2(*, rules: str = REGULATION) -> list[dict[str, Any]]:
    return [
        {
            "ticker": "KXEPL-TOT",
            "event_ticker": KALSHI_EVENT["event_ticker"],
            "title": "Tottenham vs Everton",
            "yes_sub_title": "Tottenham",
            "rules_primary": rules,
        },
        {
            "ticker": "KXEPL-DRAW",
            "event_ticker": KALSHI_EVENT["event_ticker"],
            "title": "Tottenham vs Everton",
            "yes_sub_title": "Draw",
            "rules_primary": rules,
        },
        {
            "ticker": "KXEPL-EVE",
            "event_ticker": KALSHI_EVENT["event_ticker"],
            "title": "Tottenham vs Everton",
            "yes_sub_title": "Everton",
            "rules_primary": rules,
        },
    ]


def _mb_btts() -> dict[str, Any]:
    return {
        "id": 26820,
        "name": "Both Teams To Score",
        "runners": [{"id": 1, "name": "Yes"}, {"id": 2, "name": "No"}],
    }


def _pm_btts(*, description: str = PM_REGULATION) -> dict[str, Any]:
    return {
        "id": "pm-btts",
        "question": "Both teams to score?",
        "sportsMarketType": "both teams to score",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes", "no"]',
        "description": description,
    }


def _kalshi_btts(*, rules: str = REGULATION) -> dict[str, Any]:
    return {
        "ticker": "KXEPL-BTTS",
        "event_ticker": KALSHI_EVENT["event_ticker"],
        "title": "Both Teams To Score",
        "yes_sub_title": "Yes",
        "rules_primary": rules,
    }


def _mb_totals(line: str, *, team: bool = False) -> dict[str, Any]:
    if team:
        return {
            "id": 26831,
            "name": "Tottenham Over/Under 2.5 Goals",
            "runners": [{"id": 1, "name": "Over 2.5"}, {"id": 2, "name": "Under 2.5"}],
        }
    return {
        "id": 26830 if line == "2.5" else 26832,
        "name": f"Over/Under {line} Goals",
        "line": line,
        "runners": [
            {"id": 1, "name": f"Over {line}"},
            {"id": 2, "name": f"Under {line}"},
        ],
    }


def _pm_totals(line: str, *, description: str = PM_REGULATION) -> dict[str, Any]:
    return {
        "id": f"pm-tg-{line}",
        "question": f"Total goals {line}",
        "sportsMarketType": "total goals",
        "line": line,
        "outcomes": '["Over", "Under"]',
        "clobTokenIds": '["o", "u"]',
        "description": description,
    }


def _kalshi_totals(line: str, *, rules: str = REGULATION) -> dict[str, Any]:
    return {
        "ticker": f"KXEPL-TOTAL-{line}",
        "event_ticker": KALSHI_EVENT["event_ticker"],
        "title": f"Tottenham vs Everton Total Goals {line}",
        "yes_sub_title": f"Over {line}",
        "rules_primary": rules,
        "strike": line,
    }


def _mb_ftts(*, include_no_goal: bool = True, name: str = "First Team To Score") -> dict[str, Any]:
    runners = [{"id": 1, "name": "Tottenham"}, {"id": 2, "name": "Everton"}]
    if include_no_goal:
        runners.append({"id": 3, "name": "No Goal"})
    return {"id": 26840, "name": name, "runners": runners}


def _pm_ftts(
    *,
    include_no_goal: bool = True,
    description: str = PM_REGULATION,
    player: bool = False,
) -> dict[str, Any]:
    if player:
        return {
            "id": "pm-fg",
            "question": "First Goalscorer?",
            "sportsMarketType": "first goalscorer",
            "outcomes": '["Son", "No Goal"]',
            "clobTokenIds": '["p", "n"]',
            "description": description,
        }
    if include_no_goal:
        outcomes = '["Tottenham", "Everton", "No Goal"]'
        tokens = '["h", "a", "n"]'
    else:
        outcomes = '["Tottenham", "Everton"]'
        tokens = '["h", "a"]'
    return {
        "id": "pm-ftts",
        "question": "First team to score",
        "sportsMarketType": "first team to score",
        "outcomes": outcomes,
        "clobTokenIds": tokens,
        "description": description,
    }


def _kalshi_ftts(*, rules: str = REGULATION, include_no_goal: bool = True) -> list[dict[str, Any]]:
    markets = [
        {
            "ticker": "FTTS-H",
            "title": "First team to score",
            "yes_sub_title": "Tottenham",
            "rules_primary": rules,
        },
        {
            "ticker": "FTTS-A",
            "title": "First team to score",
            "yes_sub_title": "Everton",
            "rules_primary": rules,
        },
    ]
    if include_no_goal:
        markets.append(
            {
                "ticker": "FTTS-N",
                "title": "First team to score",
                "yes_sub_title": "No Goal",
                "rules_primary": rules,
            }
        )
    return markets


def census_corpus() -> tuple[CorpusEntry, ...]:
    good = CatalogueApprovalState.APPROVED_EQUIVALENT
    review = CatalogueApprovalState.REVIEW_REQUIRED
    mismatch = CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH
    contradiction = CatalogueApprovalState.KNOWN_CONTRADICTION
    unsupported = CatalogueApprovalState.UNSUPPORTED
    return (
        CorpusEntry(
            entry_id="good-1x2-mb-pm",
            archetype=CatalogueArchetype.MATCH_RESULT_1X2,
            expected_state=good,
            known_kind="known_good",
            left=_mb([_mb_1x2()]),
            right=_pm([_pm_1x2()]),
        ),
        CorpusEntry(
            entry_id="good-1x2-mb-k-complete",
            archetype=CatalogueArchetype.MATCH_RESULT_1X2,
            expected_state=good,
            known_kind="known_good",
            left=_mb([_mb_1x2()]),
            right=_kalshi(_kalshi_1x2()),
        ),
        CorpusEntry(
            entry_id="good-1x2-k-pm-complete",
            archetype=CatalogueArchetype.MATCH_RESULT_1X2,
            expected_state=good,
            known_kind="known_good",
            left=_kalshi(_kalshi_1x2()),
            right=_pm([_pm_1x2()]),
        ),
        CorpusEntry(
            entry_id="bad-1x2-mb-k-gamewin-unknown",
            archetype=CatalogueArchetype.MATCH_RESULT_1X2,
            expected_state=review,
            known_kind="known_bad",
            left=_mb([_mb_1x2()]),
            right=_kalshi(_kalshi_1x2(rules=GAMEWIN_TEMPLATE), series=KALSHI_GAMEWIN_SERIES),
            notes=["matcher_currently_admits_gamewin_unknown_1x2"],
        ),
        CorpusEntry(
            entry_id="bad-1x2-mb-pm-unknown-settlement",
            archetype=CatalogueArchetype.MATCH_RESULT_1X2,
            expected_state=review,
            known_kind="known_bad",
            left=_mb([_mb_1x2()]),
            right=_pm([_pm_1x2(description="See market rules.")]),
        ),
        CorpusEntry(
            entry_id="bad-1x2-period-mismatch",
            archetype=CatalogueArchetype.MATCH_RESULT_1X2,
            expected_state=mismatch,
            known_kind="known_bad",
            left=_mb([_mb_1x2()]),
            right=_pm([_pm_1x2(question="First half match result?")]),
        ),
        CorpusEntry(
            entry_id="bad-1x2-et-contradiction",
            archetype=CatalogueArchetype.MATCH_RESULT_1X2,
            expected_state=contradiction,
            known_kind="known_bad",
            left=_mb([_mb_1x2()]),
            right=_pm([_pm_1x2(description=ET_RULES)]),
        ),
        CorpusEntry(
            entry_id="good-btts-mb-pm",
            archetype=CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
            expected_state=good,
            known_kind="known_good",
            left=_mb([_mb_btts()]),
            right=_pm([_pm_btts()]),
        ),
        CorpusEntry(
            entry_id="good-btts-mb-k",
            archetype=CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
            expected_state=good,
            known_kind="known_good",
            left=_mb([_mb_btts()]),
            right=_kalshi([_kalshi_btts()]),
        ),
        CorpusEntry(
            entry_id="good-btts-k-pm",
            archetype=CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
            expected_state=good,
            known_kind="known_good",
            left=_kalshi([_kalshi_btts()]),
            right=_pm([_pm_btts()]),
        ),
        CorpusEntry(
            entry_id="bad-btts-k-ambiguous-rules",
            archetype=CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
            expected_state=review,
            known_kind="known_bad",
            left=_mb([_mb_btts()]),
            right=_kalshi([_kalshi_btts(rules="See contract URL.")]),
        ),
        CorpusEntry(
            entry_id="good-totals-25-mb-pm",
            archetype=CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
            expected_state=good,
            known_kind="known_good",
            left=_mb([_mb_totals("2.5")]),
            right=_pm([_pm_totals("2.5")]),
        ),
        CorpusEntry(
            entry_id="good-totals-25-mb-k",
            archetype=CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
            expected_state=good,
            known_kind="known_good",
            left=_mb([_mb_totals("2.5")]),
            right=_kalshi([_kalshi_totals("2.5")]),
        ),
        CorpusEntry(
            entry_id="good-totals-25-k-pm",
            archetype=CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
            expected_state=good,
            known_kind="known_good",
            left=_kalshi([_kalshi_totals("2.5")]),
            right=_pm([_pm_totals("2.5")]),
        ),
        CorpusEntry(
            entry_id="bad-totals-line-mismatch",
            archetype=CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
            expected_state=mismatch,
            known_kind="known_bad",
            left=_mb([_mb_totals("2.5")]),
            right=_pm([_pm_totals("3.5")]),
        ),
        CorpusEntry(
            entry_id="good-totals-20-mb-pm",
            archetype=CatalogueArchetype.TOTAL_GOALS_INTEGER,
            expected_state=good,
            known_kind="known_good",
            left=_mb([_mb_totals("2.0")]),
            right=_pm([_pm_totals("2.0")]),
        ),
        CorpusEntry(
            entry_id="bad-totals-integer-mb-k",
            archetype=CatalogueArchetype.TOTAL_GOALS_INTEGER,
            expected_state=unsupported,
            known_kind="known_bad",
            left=_mb([_mb_totals("2.0")]),
            right=_kalshi([_kalshi_totals("2.0")]),
        ),
        CorpusEntry(
            entry_id="bad-totals-team-vs-match",
            archetype=CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
            expected_state=unsupported,
            known_kind="known_bad",
            left=_mb([_mb_totals("2.5", team=True)]),
            right=_pm([_pm_totals("2.5")]),
        ),
        CorpusEntry(
            entry_id="good-ftts-mb-pm",
            archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
            expected_state=good,
            known_kind="known_good",
            left=_mb([_mb_ftts()]),
            right=_pm([_pm_ftts()]),
        ),
        CorpusEntry(
            entry_id="good-ftts-mb-k",
            archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
            expected_state=good,
            known_kind="known_good",
            left=_mb([_mb_ftts()]),
            right=_kalshi(_kalshi_ftts()),
        ),
        CorpusEntry(
            entry_id="good-ftts-k-pm",
            archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
            expected_state=good,
            known_kind="known_good",
            left=_kalshi(_kalshi_ftts()),
            right=_pm([_pm_ftts()]),
        ),
        CorpusEntry(
            entry_id="bad-ftts-missing-no-goal-both",
            archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
            expected_state=review,
            known_kind="known_bad",
            left=_mb([_mb_ftts(include_no_goal=False)]),
            right=_pm([_pm_ftts(include_no_goal=False)]),
        ),
        CorpusEntry(
            entry_id="bad-ftts-asymmetric-no-goal",
            archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
            expected_state=contradiction,
            known_kind="known_bad",
            left=_mb([_mb_ftts()]),
            right=_pm([_pm_ftts(include_no_goal=False)]),
        ),
        CorpusEntry(
            entry_id="bad-ftts-k-unproven-regulation",
            archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
            expected_state=review,
            known_kind="known_bad",
            left=_mb([_mb_ftts()]),
            right=_kalshi(_kalshi_ftts(rules="Winner of the match.")),
        ),
        CorpusEntry(
            entry_id="bad-ftts-player-goalscorer",
            archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
            expected_state=unsupported,
            known_kind="known_bad",
            left=_mb([_mb_ftts()]),
            right=_pm([_pm_ftts(player=True)]),
        ),
        CorpusEntry(
            entry_id="bad-ftts-et-contradiction",
            archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
            expected_state=contradiction,
            known_kind="known_bad",
            left=_mb([_mb_ftts()]),
            right=_pm([_pm_ftts(description=ET_RULES)]),
        ),
    )
