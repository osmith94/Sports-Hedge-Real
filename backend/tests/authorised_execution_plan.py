"""Stamp a unique accepted Price-2 snapshot onto a fill plan for tests."""

from __future__ import annotations

import json
from itertools import count

from sports_hedge.paper.chain import PaperFillPlan

_IDS = count(1)


def authorised_execution_plan(plan: PaperFillPlan, snapshot_id: str | None = None) -> PaperFillPlan:
    identifier = snapshot_id or f"test-execution-snapshot-{next(_IDS)}"
    return plan.model_copy(
        update={
            "execution_authoritative": True,
            "execution_snapshot_json": json.dumps(
                {"snapshot_id": identifier, "accepted": True}
            ),
        }
    )
