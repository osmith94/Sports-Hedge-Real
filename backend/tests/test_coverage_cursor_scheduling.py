"""Continuous BACKGROUND / HOT coverage cursor.

Synthetic clocks and membership lists. No live venue calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sports_hedge.application.coverage_cursor import CoverageCursor, SqliteCoverageCursorStore

T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _ids(count: int) -> list[str]:
    return [f"row-{index:03d}" for index in range(1, count + 1)]


def test_background_round_robin_resumes_and_wraps() -> None:
    cursor = CoverageCursor(lane="background")
    membership = _ids(300)
    now = T0
    seen: list[str] = []
    while cursor.pass_number == 1 and cursor.hold_until is None:
        claimed = cursor.claim(membership, blocked=set(), limit=24, now=now)
        if not claimed:
            break
        seen.extend(claimed)
        now += timedelta(seconds=1)
    assert seen[0] == "row-001"
    assert seen == membership
    assert cursor.hold_until is not None
    cursor.hold_for_target(target_seconds=0, now=now)
    wrapped = cursor.claim(membership, blocked=set(), limit=3, now=now)
    assert wrapped == ["row-001", "row-002", "row-003"]
    assert cursor.pass_number == 2


def test_background_preemption_keeps_cursor() -> None:
    cursor = CoverageCursor(lane="background")
    membership = _ids(300)
    first = cursor.claim(membership, blocked=set(), limit=87, now=T0)
    assert first[-1] == "row-087"
    hot = CoverageCursor(lane="hot")
    hot_rows = hot.claim(["hot-1", "hot-2"], blocked=set(), limit=10, now=T0)
    assert hot_rows == ["hot-1", "hot-2"]
    resumed = cursor.claim(membership, blocked=set(), limit=2, now=T0)
    assert resumed == ["row-088", "row-089"]


def test_failure_does_not_block_the_next_row() -> None:
    cursor = CoverageCursor(lane="background")
    membership = _ids(10)
    claimed = cursor.claim(membership, blocked={"row-003"}, limit=5, now=T0)
    assert "row-003" not in claimed
    assert claimed[0] == "row-001"
    assert "row-004" in claimed
    retried = cursor.claim(membership, blocked=set(), limit=10, now=T0)
    assert retried[0] == "row-007"
    assert "row-003" in retried


def test_hot_target_waits_when_early_and_continues_when_late() -> None:
    early = CoverageCursor(lane="hot")
    rows = _ids(20)
    started = T0
    early.claim(rows, blocked=set(), limit=20, now=started)
    assert early.hold_until is not None
    finished = started + timedelta(seconds=4)
    wait = early.hold_for_target(target_seconds=10, now=finished)
    assert 5.9 <= wait <= 6.1
    assert early.claim(rows, blocked=set(), limit=5, now=finished) == []
    nxt = early.claim(rows, blocked=set(), limit=2, now=started + timedelta(seconds=10))
    assert nxt[0] == "row-001"

    late = CoverageCursor(lane="hot")
    late.claim(rows, blocked=set(), limit=10, now=started)
    assert late.hold_until is None
    more = late.claim(rows, blocked=set(), limit=10, now=started + timedelta(seconds=18))
    assert more[0] == "row-011"
    assert late.hold_until is not None
    wait_late = late.hold_for_target(target_seconds=10, now=started + timedelta(seconds=18))
    assert wait_late == 0.0
    immediate = late.claim(rows, blocked=set(), limit=1, now=started + timedelta(seconds=18))
    assert immediate == ["row-001"]


def test_hot_membership_changes_do_not_rewind() -> None:
    cursor = CoverageCursor(lane="hot")
    first = cursor.claim(["a", "b", "c", "d"], blocked=set(), limit=2, now=T0)
    assert first == ["a", "b"]
    added = cursor.claim(["a", "b", "c", "d", "e"], blocked=set(), limit=10, now=T0)
    assert added == ["c", "d", "e"]
    cursor2 = CoverageCursor(lane="hot")
    cursor2.claim(["a", "b", "c"], blocked=set(), limit=1, now=T0)
    left = cursor2.claim(["a", "c"], blocked=set(), limit=10, now=T0)
    assert left == ["c"]
    assert "b" not in left


def test_resume_key_is_not_a_work_queue(tmp_path) -> None:
    store = SqliteCoverageCursorStore(str(tmp_path / "coverage-cursor.sqlite"))
    cursor = CoverageCursor(lane="background")
    cursor.claim(_ids(10), blocked=set(), limit=4, now=T0)
    store.save(cursor)
    restored = CoverageCursor.from_resume("background", store.load("background"))
    nxt = restored.claim(_ids(10), blocked=set(), limit=2, now=T0)
    assert nxt == ["row-005", "row-006"]
    assert "next_retry_at" not in restored.to_resume()


def test_unstarted_claims_stay_in_the_current_pass() -> None:
    cursor = CoverageCursor(lane="hot")
    rows = _ids(8)
    claimed = cursor.claim(rows, blocked=set(), limit=8, now=T0)
    assert claimed == rows
    assert cursor.hold_until is not None
    assert cursor.visited == set(rows)
    cursor.release_unstarted(rows[2:])
    assert cursor.visited == {"row-001", "row-002"}
    assert cursor.hold_until is None
    assert cursor.pass_number == 1
    nxt = cursor.claim(rows, blocked=set(), limit=8, now=T0)
    assert nxt[0] != "row-001"
    assert set(nxt) == set(rows[2:])
    assert cursor.hold_until is not None


def test_new_rows_do_not_reset_the_cursor() -> None:
    cursor = CoverageCursor(lane="background")
    cursor.claim(["b", "c", "d"], blocked=set(), limit=2, now=T0)
    nxt = cursor.claim(["a", "b", "c", "d"], blocked=set(), limit=10, now=T0)
    assert nxt[0] == "d"
    assert "a" in nxt
    assert "b" not in nxt
