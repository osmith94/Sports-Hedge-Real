"""Round-robin coverage cursors for BACKGROUND and HOT pricing.

The durable catalogue stays the membership authority. This cursor only
remembers how far the current pass has claimed rows. It is not a second
work queue: retry/backoff stays on the row, and a pass does not restart
because a timer fires.

BACKGROUND has no target wait: the next pass starts as soon as the current
one has claimed every member. HOT may wait until a target refresh boundary
after a pass finishes early. A slow pass is never reset at that boundary.

Claiming a row is not coverage. ``release_unstarted`` puts rows that never
started provider work back into the current pass. A pass finishes only when
every member was honestly completed or is still claimed as completed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CoverageCursor:
    """Forward cursor over a stable membership order."""

    lane: str
    pass_number: int = 1
    cursor_after_id: str | None = None
    visited: set[str] = field(default_factory=set)
    pending_inserts: list[str] = field(default_factory=list)
    completed_this_pass: int = 0
    catalogue_count: int = 0
    pass_started_at: datetime | None = None
    last_full_pass_seconds: float | None = None
    hold_until: datetime | None = None
    last_claimed: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, object]:
        position = self.completed_this_pass
        total = self.catalogue_count
        percent = (100.0 * position / total) if total else 0.0
        return {
            "lane": self.lane,
            "pass_number": self.pass_number,
            "cursor_after_id": self.cursor_after_id,
            "position": position,
            "catalogue_rows": total,
            "percent": round(percent, 1),
            "last_full_pass_seconds": self.last_full_pass_seconds,
            "hold_until": self.hold_until.isoformat() if self.hold_until else None,
            "running": self.hold_until is None,
        }

    def reconcile(self, membership: list[str]) -> list[str]:
        """Drop departed rows and queue new rows behind the cursor."""

        ordered = list(dict.fromkeys(membership))
        member_set = set(ordered)
        self.visited &= member_set
        self.pending_inserts = [
            row_id
            for row_id in self.pending_inserts
            if row_id in member_set and row_id not in self.visited
        ]
        if self.cursor_after_id not in member_set:
            self.cursor_after_id = _predecessor(ordered, self.cursor_after_id)
        start = _start_index(ordered, self.cursor_after_id)
        passed = set(ordered[:start])
        known = set(self.pending_inserts) | self.visited
        for row_id in ordered:
            if row_id in known:
                continue
            if row_id in passed or (start >= len(ordered) and row_id not in self.visited):
                self.pending_inserts.append(row_id)
                known.add(row_id)
        self.catalogue_count = len(ordered)
        self.completed_this_pass = len(self.visited)
        return ordered

    def claim(
        self,
        membership: list[str],
        *,
        blocked: set[str],
        limit: int,
        now: datetime,
    ) -> list[str]:
        """Claim the next unvisited region. Failures do not rewind the cursor."""

        if limit <= 0:
            return []
        if self.hold_until is not None and now < self.hold_until:
            return []
        if self.hold_until is not None and now >= self.hold_until:
            self._open_next_pass(now)
        ordered = self.reconcile(membership)
        if not ordered and not self.pending_inserts:
            self.catalogue_count = 0
            return []
        if self.pass_started_at is None:
            self.pass_started_at = now
        claimed: list[str] = []
        start = _start_index(ordered, self.cursor_after_id)
        index = start
        while len(claimed) < limit and index < len(ordered):
            row_id = ordered[index]
            index += 1
            if row_id in self.visited:
                continue
            if row_id in blocked:
                if row_id not in self.pending_inserts:
                    self.pending_inserts.append(row_id)
                continue
            claimed.append(row_id)
        if len(claimed) < limit:
            still_pending: list[str] = []
            for row_id in self.pending_inserts:
                if row_id in self.visited or row_id in blocked:
                    if row_id not in self.visited:
                        still_pending.append(row_id)
                    continue
                if len(claimed) < limit:
                    claimed.append(row_id)
                else:
                    still_pending.append(row_id)
            self.pending_inserts = still_pending
        if claimed:
            for row_id in claimed:
                self.visited.add(row_id)
            best_index = _start_index(ordered, self.cursor_after_id) - 1
            best = self.cursor_after_id
            for row_id in claimed:
                if row_id not in ordered:
                    continue
                index = ordered.index(row_id)
                if index > best_index:
                    best = row_id
                    best_index = index
            self.cursor_after_id = best
            self.completed_this_pass = len(self.visited)
            self.last_claimed = list(claimed)
            self.catalogue_count = len(ordered)
            if not self._any_unvisited(ordered):
                self._finish_pass(now)
            return claimed
        if self._any_unvisited(ordered):
            return []
        self._finish_pass(now)
        return []

    def release_unstarted(self, row_ids: list[str]) -> None:
        """Return claims that never started work to the current pass.

        ``claim`` marks rows visited so a concurrent claim cannot take them
        twice. That mark is not coverage. Rows that end as
        ``not_started_this_cadence`` are removed from ``visited`` and queued
        behind the cursor via ``pending_inserts``. If the claim had closed
        the pass, the pass reopens. The next claim continues forward, then
        picks these rows up before a new pass starts.
        """

        if not row_ids:
            return
        released = False
        for row_id in row_ids:
            if row_id in self.visited:
                self.visited.discard(row_id)
                released = True
            if row_id not in self.visited and row_id not in self.pending_inserts:
                self.pending_inserts.append(row_id)
        if not released:
            return
        self.completed_this_pass = len(self.visited)
        if self.hold_until is not None:
            self.hold_until = None

    def hold_for_target(self, *, target_seconds: float, now: datetime) -> float:
        """Arm the post-pass wait. Returns seconds until the next pass may start."""

        if self.pass_started_at is None or self.hold_until is None:
            return 0.0
        elapsed = max(0.0, (now - self.pass_started_at).total_seconds())
        self.last_full_pass_seconds = elapsed
        remaining = float(target_seconds) - elapsed
        if remaining <= 0:
            self._open_next_pass(now)
            return 0.0
        self.hold_until = self.pass_started_at + _seconds(target_seconds)
        if self.hold_until <= now:
            self._open_next_pass(now)
            return 0.0
        return max(0.0, (self.hold_until - now).total_seconds())

    def _any_unvisited(self, ordered: list[str]) -> bool:
        for row_id in ordered:
            if row_id not in self.visited:
                return True
        for row_id in self.pending_inserts:
            if row_id not in self.visited:
                return True
        return False

    def _unvisited_unblocked(self, ordered: list[str], blocked: set[str]) -> bool:
        for row_id in ordered:
            if row_id not in self.visited and row_id not in blocked:
                return True
        for row_id in self.pending_inserts:
            if row_id not in self.visited and row_id not in blocked:
                return True
        return False

    def _finish_pass(self, now: datetime) -> None:
        if self.pass_started_at is not None:
            self.last_full_pass_seconds = max(
                0.0, (now - self.pass_started_at).total_seconds()
            )
        self.hold_until = now
        self.completed_this_pass = self.catalogue_count

    def _open_next_pass(self, now: datetime) -> None:
        self.pass_number += 1
        self.visited.clear()
        self.pending_inserts.clear()
        self.cursor_after_id = None
        self.completed_this_pass = 0
        self.pass_started_at = now
        self.hold_until = None
        self.last_claimed = []

    def to_resume(self) -> dict[str, object]:
        return {
            "pass_number": self.pass_number,
            "cursor_after_id": self.cursor_after_id,
            "visited": sorted(self.visited),
            "pending_inserts": list(self.pending_inserts),
            "completed_this_pass": self.completed_this_pass,
            "catalogue_count": self.catalogue_count,
            "pass_started_at": self.pass_started_at.isoformat() if self.pass_started_at else None,
            "last_full_pass_seconds": self.last_full_pass_seconds,
        }

    @classmethod
    def from_resume(cls, lane: str, payload: dict[str, object] | None) -> CoverageCursor:
        cursor = cls(lane=lane)
        if not payload:
            return cursor
        cursor.pass_number = int(payload.get("pass_number") or 1)
        raw_after = payload.get("cursor_after_id")
        cursor.cursor_after_id = str(raw_after) if raw_after else None
        visited = payload.get("visited") or []
        if isinstance(visited, list):
            cursor.visited = {str(item) for item in visited}
        pending = payload.get("pending_inserts") or []
        if isinstance(pending, list):
            cursor.pending_inserts = [str(item) for item in pending]
        cursor.completed_this_pass = int(payload.get("completed_this_pass") or len(cursor.visited))
        cursor.catalogue_count = int(payload.get("catalogue_count") or 0)
        started = payload.get("pass_started_at")
        if isinstance(started, str) and started:
            cursor.pass_started_at = datetime.fromisoformat(started)
        last = payload.get("last_full_pass_seconds")
        cursor.last_full_pass_seconds = float(last) if last is not None else None
        return cursor


def _predecessor(ordered: list[str], missing: str | None) -> str | None:
    if not ordered or not missing:
        return None
    prior = None
    for row_id in ordered:
        if row_id > missing:
            return prior
        prior = row_id
    return prior


def _start_index(ordered: list[str], cursor_after_id: str | None) -> int:
    if cursor_after_id is None:
        return 0
    if cursor_after_id in ordered:
        return ordered.index(cursor_after_id) + 1
    for index, row_id in enumerate(ordered):
        if row_id > cursor_after_id:
            return index
    return len(ordered)


def _seconds(value: float):
    from datetime import timedelta

    return timedelta(seconds=value)


class SqliteCoverageCursorStore:
    """Optional process-restart resume for a coverage cursor.

    Production does not construct this store. It persists pass position only
    (cursor, visited, pending inserts). It does not store retry times, in-flight
    flags, or a durable pricing queue.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self, lane: str) -> dict[str, object] | None:
        import json
        import sqlite3

        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS coverage_cursor (
                    lane TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT payload FROM coverage_cursor WHERE lane = ?",
                (lane,),
            ).fetchone()
        if row is None:
            return None
        loaded = json.loads(str(row[0]))
        return loaded if isinstance(loaded, dict) else None

    def save(self, cursor: CoverageCursor) -> None:
        import json
        import sqlite3

        payload = json.dumps(cursor.to_resume())
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS coverage_cursor (
                    lane TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO coverage_cursor (lane, payload)
                VALUES (?, ?)
                ON CONFLICT(lane) DO UPDATE SET payload = excluded.payload
                """,
                (cursor.lane, payload),
            )
