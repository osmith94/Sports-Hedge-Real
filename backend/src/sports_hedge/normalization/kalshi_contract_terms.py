"""Bounded Kalshi series contract-terms family metadata.

Follows documented ``series.contract_terms_url`` only. No per-market PDF
parsing and no GAME/Opta/title inference. Settlement applies a family default
only when the catalog records an unambiguous default that applies to the
market family; SOCCERGAMEWIN has none.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlparse

from sports_hedge.normalization.text import normalize_text

KALSHI_CONTRACT_TERMS_HOSTS = frozenset(
    {
        "assets.kalshi.com",
        "kalshi-public-docs.s3.amazonaws.com",
    }
)
KALSHI_CONTRACT_TERMS_PATH_PREFIX = "/contract_terms/"
KALSHI_CONTRACT_TERMS_MAX_BYTES = 2_000_000
_DEFAULT_CLAUSE_RE = re.compile(
    r"where not specified otherwise.{0,80}shall be understood to refer to"
)
_REGULATION_DEFAULT_RE = re.compile(
    r"shall be understood to refer to regulation time only"
)
_ET_SUM_DEFAULT_RE = re.compile(
    r"shall be understood to refer to the sum of regulation time and extra time"
)
_SPECIFIED_BY_EXCHANGE_RE = re.compile(r"specified by the exchange")
_LISTED_SCOPE_LABELS = (
    ("first_half", "first half"),
    ("regulation_time", "regulation time"),
    ("second_half", "second half"),
    ("extra_time", "extra time"),
    ("full_match", "full match"),
)

# Live bytes observed 2026-09-17 from the public contract_terms_url for KXEPLGAME.
SOCCERGAMEWIN_SHA256 = "3f1d6cc1765afa3eb44d809107f24b939dcc68f71718dc38ffe13d354c3f4ce2"
SOCCEREXACTSCORE_SHA256 = "1b630a064ad95f82de06ec46de7f4c3f24dd9b1724b826f47faf3989fea83e02"
SOCCERANYGOAL_SHA256 = "f8109150c0aca60ce494af93e528190636fc4fbb7f30b476e9e57ce63e68ff8d"

KALSHI_CONTRACT_FAMILIES: dict[str, dict[str, Any]] = {
    "soccergamewin": {
        "family_id": "soccergamewin",
        "rulebook": "SOCCERGAMEWIN",
        "official_product_name_kind": "will_team_win",
        "defines_default_result_scope": False,
        "default_result_scope": None,
        # GAME/WIN is a Match Result family, but it has no default <result scope>.
        "default_applies_to_match_result": False,
        "placeholder_specified_by_exchange": True,
        "listed_result_scopes": [
            "first_half",
            "regulation_time",
            "second_half",
            "extra_time",
            "full_match",
        ],
        "catalog_version": "2026-05-19",
        "sha256": SOCCERGAMEWIN_SHA256,
        "filenames": ("SOCCERGAMEWIN.pdf",),
    },
    "soccerexactscore": {
        "family_id": "soccerexactscore",
        "rulebook": "SOCCEREXACTSCORE",
        "official_product_name_kind": "exact_score",
        "defines_default_result_scope": True,
        "default_result_scope": "regulation_time",
        "default_applies_to_match_result": False,
        "placeholder_specified_by_exchange": True,
        "listed_result_scopes": ["regulation_time", "extra_time", "full_match"],
        "catalog_version": "public-terms",
        "sha256": SOCCEREXACTSCORE_SHA256,
        "filenames": ("SOCCEREXACTSCORE.pdf",),
    },
    "socceranygoal": {
        "family_id": "socceranygoal",
        "rulebook": "SOCCERANYGOAL",
        "official_product_name_kind": "any_goal",
        "defines_default_result_scope": True,
        "default_result_scope": "including_extra_time",
        "default_applies_to_match_result": False,
        "placeholder_specified_by_exchange": True,
        "listed_result_scopes": ["regulation_time", "extra_time", "full_match"],
        "catalog_version": "public-terms",
        "sha256": SOCCERANYGOAL_SHA256,
        "filenames": ("SOCCERANYGOAL.pdf", "SOCCERTOTAL.pdf"),
    },
}


def kalshi_contract_terms_url_is_allowlisted(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").casefold()
    path = parsed.path or ""
    if host not in KALSHI_CONTRACT_TERMS_HOSTS:
        return False
    if not path.startswith(KALSHI_CONTRACT_TERMS_PATH_PREFIX):
        return False
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        return False
    filename = path.rsplit("/", 1)[-1]
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,80}\.pdf", filename))


def kalshi_contract_terms_url_filename(url: str) -> str | None:
    if not kalshi_contract_terms_url_is_allowlisted(url):
        return None
    return urlparse(str(url).strip()).path.rsplit("/", 1)[-1]


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def classify_kalshi_contract_terms_text(text: str) -> dict[str, Any]:
    """SAFE semantics of public contract-terms wording. Never returns the text."""

    normalized = normalize_text(text)
    if not normalized:
        return {
            "has_default_clause": False,
            "default_result_scope": None,
            "placeholder_specified_by_exchange": False,
            "listed_result_scopes": [],
            "looks_like_soccergamewin": False,
        }
    has_default = bool(_DEFAULT_CLAUSE_RE.search(normalized))
    default: str | None = None
    if has_default and _REGULATION_DEFAULT_RE.search(normalized):
        default = "regulation_time"
    elif has_default and _ET_SUM_DEFAULT_RE.search(normalized):
        default = "including_extra_time"
    elif has_default:
        default = "unknown"
    listed = [key for key, label in _LISTED_SCOPE_LABELS if label in normalized]
    listed_five = {key for key, _label in _LISTED_SCOPE_LABELS}.issubset(set(listed))
    return {
        "has_default_clause": has_default,
        "default_result_scope": default,
        "placeholder_specified_by_exchange": bool(_SPECIFIED_BY_EXCHANGE_RE.search(normalized)),
        "listed_result_scopes": listed,
        "looks_like_soccergamewin": listed_five and not has_default,
    }


def lookup_kalshi_contract_family(
    *,
    url: str = "",
    sha256: str | None = None,
) -> dict[str, Any]:
    """Resolve cached family metadata from allowlisted URL and/or content hash."""

    sha = str(sha256 or "").strip().casefold() or None
    filename = kalshi_contract_terms_url_filename(url)
    matched: dict[str, Any] | None = None
    verified = "none"
    if sha:
        for item in KALSHI_CONTRACT_FAMILIES.values():
            if str(item.get("sha256") or "").casefold() == sha:
                matched = item
                verified = "sha256"
                break
        if matched is None:
            return _unknown_family(
                url=url,
                sha256=sha,
                verified="hash_mismatch",
                filename=filename,
            )
    if matched is None and filename:
        for item in KALSHI_CONTRACT_FAMILIES.values():
            names = {str(name).casefold() for name in item.get("filenames") or ()}
            if filename.casefold() in names:
                matched = item
                verified = "url_filename"
                break
    if matched is None:
        return _unknown_family(url=url, sha256=sha, verified=verified, filename=filename)
    return _safe_family_view(matched, url=url, sha256=sha, verified=verified, filename=filename)


def kalshi_contract_family_match_result_default(
    family: dict[str, Any] | None,
) -> str | None:
    """Return a default result-scope only when catalog says it applies to 1X2."""

    if not isinstance(family, dict):
        return None
    if family.get("defines_default_result_scope") is not True:
        return None
    if family.get("default_applies_to_match_result") is not True:
        return None
    default = family.get("default_result_scope")
    if default in {"regulation_time", "including_extra_time", "including_penalties"}:
        return str(default)
    return None


def kalshi_apply_match_result_family_default(
    family: dict[str, Any] | None,
) -> str | None:
    """Apply a 1X2 family default only after sha256 verification of the PDF bytes."""

    if not isinstance(family, dict):
        return None
    if family.get("verified") != "sha256":
        return None
    return kalshi_contract_family_match_result_default(family)


def _safe_family_view(
    item: dict[str, Any],
    *,
    url: str,
    sha256: str | None,
    verified: str,
    filename: str | None,
) -> dict[str, Any]:
    return {
        "family_id": item["family_id"],
        "rulebook": item["rulebook"],
        "official_product_name_kind": item["official_product_name_kind"],
        "defines_default_result_scope": bool(item["defines_default_result_scope"]),
        "default_result_scope": item.get("default_result_scope"),
        "default_applies_to_match_result": bool(item["default_applies_to_match_result"]),
        "placeholder_specified_by_exchange": bool(item["placeholder_specified_by_exchange"]),
        "listed_result_scopes": list(item.get("listed_result_scopes") or []),
        "catalog_version": item.get("catalog_version"),
        "verified": verified,
        "sha256_prefix": (sha256 or "")[:12] or None,
        "filename": filename,
        "url_allowlisted": kalshi_contract_terms_url_is_allowlisted(url),
        "match_result_default_scope": kalshi_contract_family_match_result_default(item)
        or "none",
    }


def _unknown_family(
    *,
    url: str,
    sha256: str | None,
    verified: str,
    filename: str | None,
) -> dict[str, Any]:
    return {
        "family_id": "unknown",
        "rulebook": None,
        "official_product_name_kind": None,
        "defines_default_result_scope": False,
        "default_result_scope": None,
        "default_applies_to_match_result": False,
        "placeholder_specified_by_exchange": False,
        "listed_result_scopes": [],
        "catalog_version": None,
        "verified": verified,
        "sha256_prefix": (sha256 or "")[:12] or None,
        "filename": filename,
        "url_allowlisted": kalshi_contract_terms_url_is_allowlisted(url),
        "match_result_default_scope": "none",
    }
