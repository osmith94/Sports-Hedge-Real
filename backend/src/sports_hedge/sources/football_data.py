from __future__ import annotations

import csv
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from sports_hedge.facts.identity import KickoffPrecision

BACKFILL_DIVISIONS = ("E0", "E1")
BACKFILL_SEASON_CODES = ("2122", "2223", "2324", "2425", "2526")

DIVISION_COMPETITION_ID = {
    "E0": "premier-league",
    "E1": "championship",
    "SP1": "la-liga",
}

KICKOFF_TIMEZONES = {
    "E0": ZoneInfo("Europe/London"),
    "E1": ZoneInfo("Europe/London"),
    "SP1": ZoneInfo("Europe/Madrid"),
}

EXPECTED_SEASON_MATCHES = {"E0": 380, "E1": 552}
MIN_SEASON_MATCHES = {"E0": 300, "E1": 400}
MAX_MAPPING_EXCEPTIONS = 5
EXPECTED_FIVE_SEASON_MATCHES = 4660

_USER_AGENT = "Sports-Hedge-research/1.0 (historical-backfill; research-only)"


class FootballDataFileError(ValueError):
    """Raised when a Football-Data file is empty, truncated, or unusable."""


def season_label_from_code(code: str) -> str:
    if len(code) != 4 or not code.isdigit():
        raise FootballDataFileError(f"Unsupported Football-Data season code {code!r}")
    start = 2000 + int(code[:2])
    end = int(code[2:])
    return f"{start}/{end:02d}"


def season_code_from_label(label: str) -> str:
    compact = label.strip().replace("–", "/").replace("-", "/")
    start_raw, end_raw = compact.split("/", 1)
    start = int(start_raw)
    end = int(end_raw)
    if end < 100:
        end = (start // 100) * 100 + end
    return f"{str(start)[2:]}{str(end)[2:]}"


def public_csv_url(div: str, season_code: str) -> str:
    """Explicit public season/division file. No hidden endpoints."""

    return f"https://www.football-data.co.uk/mmz4281/{season_code}/{div}.csv"


# Compatibility alias used by older call sites / docs.
FOOTBALL_DATA_PUBLIC_FILES = {
    "E0": public_csv_url("E0", "2526"),
    "E1": public_csv_url("E1", "2526"),
    "SP1": public_csv_url("SP1", "2526"),
}


def normalize_csv_text(csv_text: str) -> str:
    return csv_text.lstrip("\ufeff")


def rows_from_csv(csv_text: str) -> list[dict[str, str]]:
    """Parse a football-data.co.uk CSV, stripping a UTF-8 BOM on the Div header."""

    text = normalize_csv_text(csv_text)
    reader = csv.DictReader(StringIO(text))
    if not reader.fieldnames:
        raise FootballDataFileError("Football-Data CSV has no header row")
    required = {"Div", "Date", "HomeTeam", "AwayTeam"}
    names = {_header(name) for name in reader.fieldnames}
    if not required.issubset(names):
        raise FootballDataFileError(
            f"Football-Data CSV is missing required columns {sorted(required - names)}"
        )
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {_header(key): (value or "").strip() for key, value in raw.items()}
        if not row.get("HomeTeam") and not row.get("AwayTeam"):
            continue
        rows.append(row)
    return rows


def validate_season_rows(rows: list[dict[str, str]], *, div: str, min_rows: int | None = None) -> None:
    if not rows:
        raise FootballDataFileError(f"Football-Data {div} file is empty")
    floor = MIN_SEASON_MATCHES[div] if min_rows is None else min_rows
    if len(rows) < floor:
        raise FootballDataFileError(
            f"Football-Data {div} looks truncated: {len(rows)} rows (minimum {floor})"
        )
    unexpected = {row.get("Div") for row in rows if row.get("Div") and row.get("Div") != div}
    if unexpected:
        raise FootballDataFileError(f"Football-Data {div} contains other Div values: {sorted(unexpected)}")


def parse_kickoff(row: dict[str, str], div: str) -> tuple[datetime | None, KickoffPrecision]:
    """Convert source-local Date/Time to UTC. Timezone is documented per division."""

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
    tzinfo = KICKOFF_TIMEZONES.get(div, ZoneInfo("UTC"))
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
    local = datetime(year, month, day, hour, minute, tzinfo=tzinfo)
    return local.astimezone(UTC), precision


def int_or_none(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def fetch_public_csv(div: str, season_code: str, *, timeout: float = 60.0) -> tuple[str, str]:
    """GET the published season CSV. No scraping of HTML or hidden endpoints."""

    url = public_csv_url(div, season_code)
    response = httpx.get(
        url,
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/csv,text/plain,*/*"},
        follow_redirects=True,
    )
    response.raise_for_status()
    return url, response.text


def load_csv_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8-sig")


def local_csv_path(local_dir: Path, season_code: str, div: str) -> Path:
    nested = local_dir / season_code / f"{div}.csv"
    if nested.exists():
        return nested
    flat = local_dir / f"{season_code}_{div}.csv"
    if flat.exists():
        return flat
    legacy = local_dir / f"{div}.csv"
    if legacy.exists() and season_code == "2526":
        return legacy
    return nested


def _header(name: str | None) -> str:
    return (name or "").lstrip("\ufeff").strip()
