from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sports_hedge.odds.models import RawOddsRecord


class OddsSourceAdapter(Protocol):
    """Provider-neutral historical odds adapter.

    Concrete adapters must only use public or authorised access. They must not
    scrape behind logins, robots, CAPTCHA, paywalls or geoblocks.
    """

    @property
    def source(self) -> str: ...

    @property
    def required(self) -> bool: ...

    def fetch(self) -> Sequence[RawOddsRecord]: ...
