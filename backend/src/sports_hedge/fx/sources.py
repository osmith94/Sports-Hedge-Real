"""Parse and fetch ECB eurofxref and BoE Sterling check series."""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol

import httpx

from sports_hedge.fx.models import PublishedFxClose, quantize_rate

ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_NS = {
    "gesmes": "http://www.gesmes.org/xml/2002-08-01",
    "ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref",
}
BOE_XUDLUSS = "XUDLUSS"
BOE_CSV_URL = (
    "https://www.bankofengland.co.uk/boeapps/database/fromshowcolumns.asp"
    "?csv.x=yes&SeriesCodes=XUDLUSS&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N"
)


class HttpGetter(Protocol):
    def get(self, url: str, *, timeout: float = 30.0) -> httpx.Response: ...


def parse_ecb_eurofxref_daily(xml_text: str, *, retrieved_at: datetime) -> list[PublishedFxClose]:
    """Derive GBP-per-unit from ECB EUR quotes. Never invent 1:1."""

    root = ET.fromstring(xml_text)
    dated = None
    for cube in root.findall(".//{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}Cube"):
        time_value = cube.attrib.get("time")
        if time_value:
            dated = cube
            break
    if dated is None:
        raise ValueError("ECB eurofxref payload has no dated Cube")
    source_date = date.fromisoformat(dated.attrib["time"])
    per_eur: dict[str, Decimal] = {}
    for child in list(dated):
        currency = child.attrib.get("currency")
        rate = child.attrib.get("rate")
        if currency and rate:
            per_eur[currency.upper()] = Decimal(rate)
    if "GBP" not in per_eur:
        raise ValueError("ECB eurofxref payload is missing GBP")
    gbp_per_eur = per_eur["GBP"]
    closes: list[PublishedFxClose] = []
    for currency, eur_per_unit in per_eur.items():
        if currency == "GBP":
            continue
        if eur_per_unit <= 0:
            raise ValueError(f"ECB rate for {currency} is not positive")
        gbp_per_unit = quantize_rate(gbp_per_eur / eur_per_unit)
        closes.append(
            PublishedFxClose(
                currency=currency,
                gbp_per_unit=gbp_per_unit,
                source_date=source_date,
                retrieved_at=retrieved_at,
                source="ecb_eurofxref",
                source_id=f"ecb_eurofxref:{source_date.isoformat()}:{currency}",
                raw={
                    "quote_currency": "EUR",
                    "usd_or_foreign_per_eur": str(eur_per_unit),
                    "gbp_per_eur": str(gbp_per_eur),
                    "source_url": ECB_DAILY_URL,
                },
            )
        )
    return closes


def parse_boe_xudluss_csv(csv_text: str, *, retrieved_at: datetime) -> list[PublishedFxClose]:
    """BoE XUDLUSS is USD per GBP. Convert to GBP per USD for the check."""

    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    if not reader.fieldnames:
        raise ValueError("BoE CSV has no header")
    date_key = next(
        (name for name in reader.fieldnames if name and name.lower() in {"date", "observation date"}),
        reader.fieldnames[0],
    )
    series_key = next(
        (name for name in reader.fieldnames if name and name.upper() == BOE_XUDLUSS),
        None,
    )
    if series_key is None:
        raise ValueError("BoE CSV is missing XUDLUSS")
    closes: list[PublishedFxClose] = []
    for row in reader:
        raw_date = (row.get(date_key) or "").strip().strip('"')
        raw_value = (row.get(series_key) or "").strip().strip('"')
        if not raw_date or not raw_value or raw_value.upper() in {"NA", "N/A", "."}:
            continue
        source_date = _parse_boe_date(raw_date)
        usd_per_gbp = Decimal(raw_value)
        if usd_per_gbp <= 0:
            raise ValueError("BoE XUDLUSS is not positive")
        gbp_per_usd = quantize_rate(Decimal("1") / usd_per_gbp)
        closes.append(
            PublishedFxClose(
                currency="USD",
                gbp_per_unit=gbp_per_usd,
                source_date=source_date,
                retrieved_at=retrieved_at,
                source="boe_xudluss",
                source_id=f"boe_xudluss:{source_date.isoformat()}:USD",
                raw={
                    "series": BOE_XUDLUSS,
                    "usd_per_gbp": str(usd_per_gbp),
                    "source_url": BOE_CSV_URL,
                },
            )
        )
    return closes


def _parse_boe_date(value: str) -> date:
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognised BoE date: {value!r}")


class EcbEurofxrefSource:
    def fetch(self, http: HttpGetter, *, retrieved_at: datetime) -> list[PublishedFxClose]:
        response = http.get(ECB_DAILY_URL, timeout=30.0)
        response.raise_for_status()
        return parse_ecb_eurofxref_daily(response.text, retrieved_at=retrieved_at)


class BoeXudlussSource:
    def fetch(self, http: HttpGetter, *, retrieved_at: datetime) -> list[PublishedFxClose]:
        response = http.get(BOE_CSV_URL, timeout=30.0)
        response.raise_for_status()
        return parse_boe_xudluss_csv(response.text, retrieved_at=retrieved_at)
