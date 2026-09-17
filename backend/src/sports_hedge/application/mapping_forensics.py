"""Safe mapping forensics over a CollectionReport.

Prints only mapping metadata. Never includes credentials, tokens, session ids,
order-book prices/sizes, or write-path data.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    VenueMarketFacts,
)
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.normalization.text import normalize_text

VENUE_SCOPE_UNIVERSE = "universe_lane_participation"
VENUE_SCOPE_ALL = "all_operator_venues_forensic"
COMPLETE_3WAY = "complete_3way_home_draw_away"
INCOMPLETE_BINARY = "incomplete_or_binary_yes_no"
INCOMPLETE_OTHER = "incomplete_other"
THREE_WAY_OUTCOMES = {
    CanonicalOutcome.HOME.value,
    CanonicalOutcome.DRAW.value,
    CanonicalOutcome.AWAY.value,
}
BINARY_YES_NO = {CanonicalOutcome.YES.value, CanonicalOutcome.NO.value}
PRICE_FIELD_NAMES = (
    "decimal_odds",
    "size_at_touch",
    "usable_depth_at_touch",
    "best_backs",
    "current_net_edge",
    "bids",
    "asks",
    "available-amount",
    "available_amount",
)
MATCHER_REASON_KEYS = (
    "market_family_mismatch",
    "period_mismatch",
    "line_mismatch",
    "incomplete_settlement",
    "settlement_mismatch",
    "outcome_space_mismatch",
)
SECRET_FRAGMENTS = ("password", "username", "token", "session", "mfa", "authorization", "secret")


class SafeVenueMarketView(BaseModel):
    venue: str
    raw_market_name: str | None = None
    raw_market_type: str | None = None
    raw_runner_labels: list[str] = Field(default_factory=list)
    family: str | None = None
    period: str | None = None
    line: str | None = None
    settlement_scope: str | None = None
    settlement_key: str | None = None
    settlement_complete: bool | None = None
    outcome_space: list[str] = Field(default_factory=list)
    match_result_shape: str | None = None


class SafeCandidateView(BaseModel):
    fixture_label: str
    family: str | None = None
    period: str | None = None
    line: str | None = None
    comparison_status: str | None = None
    matcher_reasons: list[str] = Field(default_factory=list)
    venues: list[SafeVenueMarketView] = Field(default_factory=list)


class MatchResultVenueCounts(BaseModel):
    complete_3way_home_draw_away: int = 0
    incomplete_or_binary_yes_no: int = 0
    incomplete_other: int = 0
    total: int = 0


class MatchbookKalshiMatchResultAssessment(BaseModel):
    """Matchbook↔Kalshi ordinary 1X2 coverage. Polymarket is excluded."""

    cross_venue_fixtures_with_both_match_result: int = 0
    both_complete_3way: int = 0
    both_settlement_complete: int = 0
    matched_equivalent: int = 0
    sample: list[SafeCandidateView] = Field(default_factory=list)


class KalshiRuleFieldView(BaseModel):
    """SAFE classification of one documented rule field. Never includes wording."""

    field: str
    present_nonempty: bool = False
    classified_scope: str | None = None
    economically_complete: bool | None = None
    wording_kind: str | None = None
    has_regulation_tokens: bool = False
    has_extra_time_tokens: bool = False
    has_penalties_tokens: bool = False
    has_ninety_minute_abbrev: bool = False
    contains_result_scope_placeholder: bool = False
    contains_payout_criterion: bool = False
    contains_multiple_scope_definitions: bool = False
    looks_like_generic_contract_template: bool = False
    clause_token_pattern: str | None = None


class KalshiRuleLayerView(BaseModel):
    """SAFE classification of one documented Kalshi rule source layer."""

    layer: str
    rules_primary_nonempty: bool = False
    rules_secondary_nonempty: bool = False
    rules_nonempty: bool = False
    any_rule_field_nonempty: bool = False
    classified_scope: str | None = None
    economically_complete: bool | None = None
    wording_kind: str | None = None
    has_regulation_tokens: bool = False
    has_extra_time_tokens: bool = False
    has_penalties_tokens: bool = False
    has_ninety_minute_abbrev: bool = False
    fetch_status: str | None = None
    contract_terms_url_present: bool | None = None
    settlement_sources_present: bool | None = None
    fields: list[KalshiRuleFieldView] = Field(default_factory=list)
    precedence_classified_scope: str | None = None
    precedence_economically_complete: bool | None = None
    fingerprint_classified_scope: str | None = None
    fingerprint_economically_complete: bool | None = None
    fingerprint_uses_rule_precedence: bool = False
    documented_selector_presence: dict[str, bool] = Field(default_factory=dict)
    structured_fields: dict[str, Any] = Field(default_factory=dict)


class KalshiMatchResultRuleTickerView(BaseModel):
    """SAFE per-ticker enrichment diagnostic. Never includes contract text."""

    fixture_label: str
    ticker: str
    skipped_because_complete: bool = False
    get_market_called: bool = False
    get_market_status: str | None = None
    layers: list[KalshiRuleLayerView] = Field(default_factory=list)


class MappingForensics(BaseModel):
    """Owner-live/read-only mapping forensics. Fixture/demo when built from tests."""

    data_class: str
    venue_scope: str
    enabled_venues: list[str] = Field(default_factory=list)
    participation_source: str | None = None
    participation_db_path: str | None = None
    operator_row_present: bool | None = None
    fixture_filter: list[str] = Field(default_factory=list)
    match_result_by_venue: dict[str, MatchResultVenueCounts] = Field(default_factory=dict)
    candidate_rejection_histogram: dict[str, int] = Field(default_factory=dict)
    cross_venue_match_result_candidates: int = 0
    reported_candidates: int = 0
    candidates: list[SafeCandidateView] = Field(default_factory=list)
    matchbook_kalshi_match_result: MatchbookKalshiMatchResultAssessment = Field(
        default_factory=MatchbookKalshiMatchResultAssessment
    )
    kalshi_match_result_rule_enrichment: dict[str, int] = Field(default_factory=dict)
    get_market_status_histogram: dict[str, int] = Field(default_factory=dict)
    get_market_wording_kind_histogram: dict[str, int] = Field(default_factory=dict)
    kalshi_rule_layers: list[KalshiMatchResultRuleTickerView] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def parse_fixture_filter(raw: str | None) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    parts: list[str] = []
    for chunk in text.replace(";", "/").replace("|", "/").split("/"):
        for item in chunk.split(","):
            token = " ".join(item.strip().split())
            if token:
                parts.append(token)
    return parts


def fixture_label_matches_filter(label: str, tokens: list[str]) -> bool:
    if not tokens:
        return True
    haystack = normalize_text(label)
    return all(normalize_text(token) in haystack for token in tokens)


def is_cross_venue_fixture(fixture: DiscoveredFixture) -> bool:
    return (
        sum(
            [
                bool(fixture.matchbook_matched),
                bool(fixture.polymarket_matched),
                bool(fixture.kalshi_matched),
            ]
        )
        >= 2
    )


def classify_match_result_shape(facts: VenueMarketFacts) -> str | None:
    if str(facts.family or "") != MarketFamily.MATCH_RESULT.value:
        return None
    outcomes = [item.casefold() for item in _outcome_space(facts)]
    present = set(outcomes)
    if THREE_WAY_OUTCOMES <= present:
        return COMPLETE_3WAY
    if present == BINARY_YES_NO or BINARY_YES_NO <= present:
        return INCOMPLETE_BINARY
    labels = {normalize_text(item) for item in facts.raw_runner_labels}
    if labels and labels <= {"yes", "no"}:
        return INCOMPLETE_BINARY
    if "draw" in labels and len(labels) >= 3:
        return COMPLETE_3WAY
    return INCOMPLETE_OTHER


def forensics_from_report(
    report: CollectionReport,
    *,
    data_class: str,
    venue_scope: str,
    enabled_venues: list[str],
    participation_source: str | None = None,
    participation_db_path: str | None = None,
    operator_row_present: bool | None = None,
    fixture_filter: list[str] | None = None,
    detail: bool = False,
    match_result_sample: int = 0,
) -> MappingForensics:
    tokens = list(fixture_filter or [])
    match_result_counts = {
        venue.value: MatchResultVenueCounts() for venue in VenueName if venue in (
            VenueName.MATCHBOOK,
            VenueName.POLYMARKET,
            VenueName.KALSHI,
        )
    }
    histogram: Counter[str] = Counter()
    all_candidates: list[SafeCandidateView] = []

    for rows in (report.fixture_markets or {}).values():
        for row in rows:
            _count_match_result_facts(row, match_result_counts)

    for fixture in report.discovered_fixtures:
        if not is_cross_venue_fixture(fixture):
            continue
        rows = list(report.fixture_markets.get(fixture.canonical_event_id) or [])
        for candidate in _candidates_sharing_family_period_line(fixture, rows):
            all_candidates.append(candidate)
            matcher_hits = [item for item in candidate.matcher_reasons if item in MATCHER_REASON_KEYS]
            for reason in matcher_hits:
                histogram[reason] += 1
            if not matcher_hits:
                histogram[
                    "matched"
                    if candidate.comparison_status == "matched_equivalent"
                    else (candidate.matcher_reasons[0] if candidate.matcher_reasons else "no_matcher_reason")
                ] += 1

    cross_venue_mr = sum(
        1 for item in all_candidates if item.family == MarketFamily.MATCH_RESULT.value
    )
    filtered = [
        item
        for item in all_candidates
        if fixture_label_matches_filter(item.fixture_label, tokens)
    ]
    sampled: list[SafeCandidateView]
    if match_result_sample > 0:
        sampled = [
            item for item in filtered if item.family == MarketFamily.MATCH_RESULT.value
        ][: max(0, int(match_result_sample))]
    elif detail:
        sampled = filtered
    elif tokens:
        sampled = [
            item for item in filtered if item.family == MarketFamily.MATCH_RESULT.value
        ][:8]
    else:
        sampled = []

    notes = [
        "PAPER MODE · EXECUTION DISABLED · mapping metadata only",
        (
            "venue_scope=universe_lane_participation mirrors the UNIVERSE operator "
            "lane. This is not a change to production venue toggles."
            if venue_scope == VENUE_SCOPE_UNIVERSE
            else "venue_scope=all_operator_venues_forensic is an explicit all-venue "
            "diagnostic and does not mirror a Polymarket-disabled UNIVERSE lane."
        ),
        "Does not output credentials, tokens, session ids, or order-book prices/sizes.",
    ]
    if participation_source == "env_default":
        notes.append(
            "participation_source=env_default: no operator UNIVERSE row was loaded "
            f"from {participation_db_path or 'paper_settings_db_path'}. A detached "
            "census process does not inherit a UI Polymarket-off toggle. "
            "Matchbook↔Kalshi metrics are reported separately."
        )
    elif operator_row_present is False:
        notes.append(
            "operator_row_present=false; UNIVERSE venues came from env defaults, "
            "not the operator UI store."
        )
    if tokens:
        notes.append("fixture_filter=" + " / ".join(tokens))
    mb_kalshi = _matchbook_kalshi_match_result_assessment(all_candidates)
    enrichment, status_hist, kind_hist, layer_views = _kalshi_rule_layer_views(
        report,
        fixture_filter=tokens,
    )
    notes.append(
        "Kalshi Get Market / nested / event / series layers report field presence "
        "and classify_settlement_wording scope only. Contract text is not printed. "
        "Series contract_terms_url is not fetched; presence is a boolean. "
        "Per-field rules_primary/rules_secondary/rules classification is reported "
        "separately from combined concatenation. Rule-field precedence is "
        "diagnostic-only and is not used as the settlement fingerprint. "
        "Official Get Market strike_type/custom_strike/market_type do not select "
        "SOCCERGAME result scope; structured custom_strike values are entity "
        "targets. Generic multi-scope template wording stays unknown."
    )
    return MappingForensics(
        data_class=data_class,
        venue_scope=venue_scope,
        enabled_venues=list(enabled_venues),
        participation_source=participation_source,
        participation_db_path=participation_db_path,
        operator_row_present=operator_row_present,
        fixture_filter=tokens,
        match_result_by_venue={
            venue: counts for venue, counts in sorted(match_result_counts.items())
        },
        candidate_rejection_histogram=dict(sorted(histogram.items())),
        cross_venue_match_result_candidates=cross_venue_mr,
        reported_candidates=len(sampled),
        candidates=sampled,
        matchbook_kalshi_match_result=mb_kalshi,
        kalshi_match_result_rule_enrichment=enrichment,
        get_market_status_histogram=status_hist,
        get_market_wording_kind_histogram=kind_hist,
        kalshi_rule_layers=layer_views,
        notes=notes,
    )


def render_forensics(forensics: MappingForensics) -> str:
    lines = [
        "OWNER-LIVE / READ-ONLY MAPPING FORENSICS",
        f"data_class={forensics.data_class}",
        f"venue_scope={forensics.venue_scope}",
        f"enabled_venues={','.join(forensics.enabled_venues) or '{}'}",
        f"participation_source={forensics.participation_source or 'n/a'}",
        f"participation_db_path={forensics.participation_db_path or 'n/a'}",
        f"operator_row_present={forensics.operator_row_present}",
        "match_result_by_venue:",
    ]
    for venue, counts in forensics.match_result_by_venue.items():
        lines.append(
            f"  {venue}: total={counts.total} complete_3way={counts.complete_3way_home_draw_away} "
            f"binary_yes_no={counts.incomplete_or_binary_yes_no} other={counts.incomplete_other}"
        )
    lines.append(
        "candidate_rejection_histogram="
        + (
            "{"
            + ", ".join(
                f"{key}={value}" for key, value in forensics.candidate_rejection_histogram.items()
            )
            + "}"
            if forensics.candidate_rejection_histogram
            else "{}"
        )
    )
    lines.append(
        f"cross_venue_match_result_candidates={forensics.cross_venue_match_result_candidates}"
    )
    lines.append(f"reported_candidates={forensics.reported_candidates}")
    mbk = forensics.matchbook_kalshi_match_result
    lines.append("matchbook_kalshi_match_result:")
    lines.append(
        f"  cross_venue_fixtures_with_both_match_result={mbk.cross_venue_fixtures_with_both_match_result}"
    )
    lines.append(f"  both_complete_3way={mbk.both_complete_3way}")
    lines.append(f"  both_settlement_complete={mbk.both_settlement_complete}")
    lines.append(f"  matched_equivalent={mbk.matched_equivalent}")
    for item in mbk.sample:
        lines.append(
            f"  sample fixture={_safe_text(item.fixture_label)} status={item.comparison_status} "
            f"matcher_reasons={','.join(item.matcher_reasons) or 'none'}"
        )
    lines.append(
        "kalshi_match_result_rule_enrichment="
        + _fmt_hist(forensics.kalshi_match_result_rule_enrichment)
    )
    lines.append(
        "get_market_status_histogram=" + _fmt_hist(forensics.get_market_status_histogram)
    )
    lines.append(
        "get_market_wording_kind_histogram="
        + _fmt_hist(forensics.get_market_wording_kind_histogram)
    )
    for item in forensics.kalshi_rule_layers:
        lines.append(
            f"kalshi_rule_layer fixture={_safe_text(item.fixture_label)} "
            f"ticker={_safe_text(item.ticker)} skipped_complete={item.skipped_because_complete} "
            f"get_market_called={item.get_market_called} get_market_status={item.get_market_status}"
        )
        for layer in item.layers:
            extra = ""
            if layer.layer == "series":
                extra = (
                    f" contract_terms_url_present={layer.contract_terms_url_present} "
                    f"settlement_sources_present={layer.settlement_sources_present}"
                )
            lines.append(
                "  "
                f"layer={layer.layer} primary={layer.rules_primary_nonempty} "
                f"secondary={layer.rules_secondary_nonempty} rules={layer.rules_nonempty} "
                f"combined_scope={layer.classified_scope} combined_complete={layer.economically_complete} "
                f"precedence_scope={layer.precedence_classified_scope} "
                f"precedence_complete={layer.precedence_economically_complete} "
                f"fingerprint_scope={layer.fingerprint_classified_scope} "
                f"fingerprint_complete={layer.fingerprint_economically_complete} "
                f"fingerprint_uses_precedence={layer.fingerprint_uses_rule_precedence} "
                f"kind={layer.wording_kind} regulation_tokens={layer.has_regulation_tokens} "
                f"et_tokens={layer.has_extra_time_tokens} pen_tokens={layer.has_penalties_tokens} "
                f"ninety_min_abbrev={layer.has_ninety_minute_abbrev} "
                f"fetch_status={layer.fetch_status}{extra}"
            )
            for field in layer.fields:
                lines.append(
                    "    "
                    f"field={field.field} present={field.present_nonempty} "
                    f"scope={field.classified_scope} complete={field.economically_complete} "
                    f"kind={field.wording_kind} regulation_tokens={field.has_regulation_tokens} "
                    f"et_tokens={field.has_extra_time_tokens} pen_tokens={field.has_penalties_tokens} "
                    f"ninety_min_abbrev={field.has_ninety_minute_abbrev} "
                    f"placeholder={field.contains_result_scope_placeholder} "
                    f"payout_criterion={field.contains_payout_criterion} "
                    f"multi_scope_defs={field.contains_multiple_scope_definitions} "
                    f"generic_template={field.looks_like_generic_contract_template} "
                    f"clause_pattern={field.clause_token_pattern}"
                )
            if layer.documented_selector_presence:
                selectors = ",".join(
                    f"{key}={value}"
                    for key, value in sorted(layer.documented_selector_presence.items())
                )
                lines.append(f"    documented_selectors={{{selectors}}}")
            if layer.structured_fields:
                lines.append(
                    "    structured_fields=" + _fmt_structured_fields(layer.structured_fields)
                )
    for note in forensics.notes:
        lines.append(f"note: {note}")
    for item in forensics.candidates:
        lines.append(
            f"candidate fixture={_safe_text(item.fixture_label)} family={item.family} "
            f"period={item.period} line={item.line or ''} status={item.comparison_status} "
            f"matcher_reasons={','.join(item.matcher_reasons) or 'none'}"
        )
        for venue in item.venues:
            lines.append(
                "  "
                f"venue={venue.venue} raw_name={_safe_text(venue.raw_market_name)} "
                f"raw_type={_safe_text(venue.raw_market_type)} "
                f"runners={venue.raw_runner_labels} family={venue.family} "
                f"period={venue.period} line={venue.line or ''} "
                f"scope={venue.settlement_scope} complete={venue.settlement_complete} "
                f"outcomes={venue.outcome_space} shape={venue.match_result_shape} "
                f"settlement_key={venue.settlement_key}"
            )
    return "\n".join(lines) + "\n"


def forensics_as_public_dict(forensics: MappingForensics) -> dict[str, Any]:
    payload = forensics.model_dump(mode="json")
    return _strip_price_fields(payload)


def _fmt_hist(values: dict[str, int]) -> str:
    if not values:
        return "{}"
    return "{" + ", ".join(f"{key}={value}" for key, value in values.items()) + "}"


def _parse_rule_field_views(raw: Any) -> list[KalshiRuleFieldView]:
    if not isinstance(raw, list):
        return []
    views: list[KalshiRuleFieldView] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        views.append(
            KalshiRuleFieldView(
                field=str(item.get("field") or "unknown"),
                present_nonempty=bool(item.get("present_nonempty")),
                classified_scope=(
                    str(item.get("classified_scope"))
                    if item.get("classified_scope") is not None
                    else None
                ),
                economically_complete=(
                    bool(item.get("economically_complete"))
                    if item.get("economically_complete") is not None
                    else None
                ),
                wording_kind=(
                    str(item.get("wording_kind")) if item.get("wording_kind") is not None else None
                ),
                has_regulation_tokens=bool(item.get("has_regulation_tokens")),
                has_extra_time_tokens=bool(item.get("has_extra_time_tokens")),
                has_penalties_tokens=bool(item.get("has_penalties_tokens")),
                has_ninety_minute_abbrev=bool(item.get("has_ninety_minute_abbrev")),
                contains_result_scope_placeholder=bool(
                    item.get("contains_result_scope_placeholder")
                ),
                contains_payout_criterion=bool(item.get("contains_payout_criterion")),
                contains_multiple_scope_definitions=bool(
                    item.get("contains_multiple_scope_definitions")
                ),
                looks_like_generic_contract_template=bool(
                    item.get("looks_like_generic_contract_template")
                ),
                clause_token_pattern=(
                    str(item.get("clause_token_pattern"))
                    if item.get("clause_token_pattern") is not None
                    else None
                ),
            )
        )
    return views


def _parse_selector_presence(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, bool] = {}
    for key, value in raw.items():
        parsed[str(key)] = bool(value)
    return parsed


_SAFE_STRUCTURED_SCALAR_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_UNSAFE_STRUCTURED_KEYS = frozenset(
    {"rules_primary", "rules_secondary", "rules", "title", "subtitle", "yes_sub_title"}
)


def _safe_structured_scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip()
        if _SAFE_STRUCTURED_SCALAR_RE.fullmatch(token):
            return token
        return "redacted"
    return None


def _parse_structured_fields(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    parsed: dict[str, Any] = {}
    for key, value in raw.items():
        name = str(key)
        if name in _UNSAFE_STRUCTURED_KEYS:
            continue
        if isinstance(value, dict):
            nested: dict[str, Any] = {}
            for nested_key, nested_value in value.items():
                token = str(nested_key)
                if not _SAFE_STRUCTURED_SCALAR_RE.fullmatch(token):
                    continue
                if isinstance(nested_value, bool | int) or nested_value is None:
                    nested[token] = nested_value
                elif isinstance(nested_value, str):
                    scalar = _safe_structured_scalar(nested_value)
                    if scalar is not None:
                        nested[token] = scalar
            parsed[name] = nested
        elif isinstance(value, list):
            items = []
            for item in value:
                scalar = _safe_structured_scalar(item)
                if scalar is not None and scalar != "redacted":
                    items.append(scalar)
            parsed[name] = items
        else:
            scalar = _safe_structured_scalar(value)
            if scalar is not None:
                parsed[name] = scalar
    return parsed


def _fmt_structured_fields(values: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in sorted(values):
        value = values[key]
        if isinstance(value, dict):
            inner = ",".join(f"{inner_key}={inner_value}" for inner_key, inner_value in sorted(value.items()))
            parts.append(f"{key}={{{inner}}}")
        elif isinstance(value, list):
            parts.append(f"{key}=[{','.join(str(item) for item in value)}]")
        else:
            parts.append(f"{key}={value}")
    return "{" + ", ".join(parts) + "}"


def _kalshi_rule_layer_views(
    report: CollectionReport,
    *,
    fixture_filter: list[str],
) -> tuple[dict[str, int], dict[str, int], dict[str, int], list[KalshiMatchResultRuleTickerView]]:
    diagnostics = dict(report.scan_diagnostics or {})
    raw_enrichment = diagnostics.get("kalshi_match_result_rule_enrichment") or {}
    enrichment: dict[str, int] = {}
    if isinstance(raw_enrichment, dict):
        for key, value in raw_enrichment.items():
            try:
                enrichment[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    records = diagnostics.get("kalshi_match_result_rule_layers") or []
    status_hist: Counter[str] = Counter()
    kind_hist: Counter[str] = Counter()
    views: list[KalshiMatchResultRuleTickerView] = []
    if not isinstance(records, list):
        records = []
    for item in records:
        if not isinstance(item, dict):
            continue
        status = str(item.get("get_market_status") or "unknown")
        status_hist[status] += 1
        layers = item.get("layers") if isinstance(item.get("layers"), list) else []
        get_market_layer = next(
            (
                layer
                for layer in layers
                if isinstance(layer, dict) and layer.get("layer") == "get_market"
            ),
            None,
        )
        kind = str((get_market_layer or {}).get("wording_kind") or "absent")
        kind_hist[kind] += 1
        label = str(item.get("fixture_label") or "")
        if fixture_filter and not fixture_label_matches_filter(label, fixture_filter):
            continue
        if not fixture_filter:
            continue
        if len(views) >= 12:
            continue
        parsed_layers: list[KalshiRuleLayerView] = []
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            parsed_layers.append(
                KalshiRuleLayerView(
                    layer=str(layer.get("layer") or "unknown"),
                    rules_primary_nonempty=bool(layer.get("rules_primary_nonempty")),
                    rules_secondary_nonempty=bool(layer.get("rules_secondary_nonempty")),
                    rules_nonempty=bool(layer.get("rules_nonempty")),
                    any_rule_field_nonempty=bool(layer.get("any_rule_field_nonempty")),
                    classified_scope=(
                        str(layer.get("classified_scope"))
                        if layer.get("classified_scope") is not None
                        else None
                    ),
                    economically_complete=(
                        bool(layer.get("economically_complete"))
                        if layer.get("economically_complete") is not None
                        else None
                    ),
                    wording_kind=(
                        str(layer.get("wording_kind"))
                        if layer.get("wording_kind") is not None
                        else None
                    ),
                    has_regulation_tokens=bool(layer.get("has_regulation_tokens")),
                    has_extra_time_tokens=bool(layer.get("has_extra_time_tokens")),
                    has_penalties_tokens=bool(layer.get("has_penalties_tokens")),
                    has_ninety_minute_abbrev=bool(layer.get("has_ninety_minute_abbrev")),
                    fetch_status=(
                        str(layer.get("fetch_status"))
                        if layer.get("fetch_status") is not None
                        else None
                    ),
                    contract_terms_url_present=(
                        bool(layer.get("contract_terms_url_present"))
                        if "contract_terms_url_present" in layer
                        else None
                    ),
                    settlement_sources_present=(
                        bool(layer.get("settlement_sources_present"))
                        if "settlement_sources_present" in layer
                        else None
                    ),
                    fields=_parse_rule_field_views(layer.get("fields")),
                    precedence_classified_scope=(
                        str(layer.get("precedence_classified_scope"))
                        if layer.get("precedence_classified_scope") is not None
                        else None
                    ),
                    precedence_economically_complete=(
                        bool(layer.get("precedence_economically_complete"))
                        if layer.get("precedence_economically_complete") is not None
                        else None
                    ),
                    documented_selector_presence=_parse_selector_presence(
                        layer.get("documented_selector_presence")
                    ),
                    fingerprint_classified_scope=(
                        str(layer.get("fingerprint_classified_scope"))
                        if layer.get("fingerprint_classified_scope") is not None
                        else None
                    ),
                    fingerprint_economically_complete=(
                        bool(layer.get("fingerprint_economically_complete"))
                        if layer.get("fingerprint_economically_complete") is not None
                        else None
                    ),
                    fingerprint_uses_rule_precedence=bool(
                        layer.get("fingerprint_uses_rule_precedence")
                    ),
                    structured_fields=_parse_structured_fields(layer.get("structured_fields")),
                )
            )
        views.append(
            KalshiMatchResultRuleTickerView(
                fixture_label=label,
                ticker=str(item.get("ticker") or ""),
                skipped_because_complete=bool(item.get("skipped_because_complete")),
                get_market_called=bool(item.get("get_market_called")),
                get_market_status=str(item.get("get_market_status") or "") or None,
                layers=parsed_layers,
            )
        )
    return (
        dict(sorted(enrichment.items())),
        dict(sorted(status_hist.items())),
        dict(sorted(kind_hist.items())),
        views,
    )


def _candidates_sharing_family_period_line(
    fixture: DiscoveredFixture,
    rows: list[FixtureMarketInventoryRow],
) -> list[SafeCandidateView]:
    """Compare markets that share family/period/line even when inventory splits them.

    Production Kalshi attach currently requires an identical settlement key, so
    a complete Matchbook 1X2 and an incomplete Kalshi GAME stay venue_only.
    Forensics still groups those candidates to show the matcher rejection.
    """

    grouped_rows = [row for row in rows if _is_grouped_candidate(row)]
    used_keys: set[tuple[str, str, str]] = set()
    candidates: list[SafeCandidateView] = []
    for row in grouped_rows:
        key = _row_group_key(row)
        if key is not None:
            used_keys.add(key)
        candidates.append(_candidate_view(fixture, row, _matcher_reasons(row)))

    by_key: dict[tuple[str, str, str], list[VenueMarketFacts]] = {}
    for row in rows:
        for facts in (row.matchbook, row.polymarket, row.kalshi):
            if facts is None or not facts.family:
                continue
            key = (
                str(facts.family),
                str(facts.period or ""),
                "" if facts.line is None else format(facts.line, "f"),
            )
            existing = by_key.setdefault(key, [])
            if any(item.venue is facts.venue for item in existing):
                continue
            existing.append(facts)

    for key, facts_list in by_key.items():
        if len(facts_list) < 2 or key in used_keys:
            continue
        reasons = _economic_reasons_from_facts_group(facts_list)
        status = "matched_equivalent" if not reasons else (
            "settlement_mismatch"
            if any(item in {"incomplete_settlement", "settlement_mismatch"} for item in reasons)
            else reasons[0]
        )
        candidates.append(
            SafeCandidateView(
                fixture_label=f"{fixture.home_team} vs {fixture.away_team}",
                family=key[0],
                period=key[1] or None,
                line=key[2] or None,
                comparison_status=status,
                matcher_reasons=reasons,
                venues=[_venue_view(item) for item in facts_list],
            )
        )
    return candidates


def _matchbook_kalshi_match_result_assessment(
    candidates: list[SafeCandidateView],
) -> MatchbookKalshiMatchResultAssessment:
    fixtures_both: set[str] = set()
    both_3way = 0
    both_complete = 0
    matched = 0
    sample: list[SafeCandidateView] = []
    for item in candidates:
        if item.family != MarketFamily.MATCH_RESULT.value:
            continue
        by_venue = {venue.venue: venue for venue in item.venues}
        mb = by_venue.get(VenueName.MATCHBOOK.value)
        kalshi = by_venue.get(VenueName.KALSHI.value)
        if mb is None or kalshi is None:
            continue
        fixtures_both.add(item.fixture_label)
        mb_3way = mb.match_result_shape == COMPLETE_3WAY
        kalshi_3way = kalshi.match_result_shape == COMPLETE_3WAY
        if mb_3way and kalshi_3way:
            both_3way += 1
        if mb.settlement_complete is True and kalshi.settlement_complete is True:
            both_complete += 1
        if item.comparison_status == "matched_equivalent":
            matched += 1
        sample.append(item)
    return MatchbookKalshiMatchResultAssessment(
        cross_venue_fixtures_with_both_match_result=len(fixtures_both),
        both_complete_3way=both_3way,
        both_settlement_complete=both_complete,
        matched_equivalent=matched,
        sample=sample[:8],
    )


def _row_group_key(row: FixtureMarketInventoryRow) -> tuple[str, str, str] | None:
    if not row.family:
        return None
    return (
        str(row.family),
        str(row.period or ""),
        "" if row.line is None else format(row.line, "f"),
    )


def _economic_reasons_from_facts_group(facts_list: list[VenueMarketFacts]) -> list[str]:
    reasons: list[str] = []
    for index, left in enumerate(facts_list):
        for right in facts_list[index + 1 :]:
            for reason in _economic_reasons_from_facts(left, right):
                if reason not in reasons:
                    reasons.append(reason)
    return reasons


def _economic_reasons_from_facts(left: VenueMarketFacts, right: VenueMarketFacts) -> list[str]:
    reasons: list[str] = []
    if left.family != right.family:
        reasons.append("market_family_mismatch")
    if (left.period or "") != (right.period or ""):
        reasons.append("period_mismatch")
    left_line = "" if left.line is None else format(left.line, "f")
    right_line = "" if right.line is None else format(right.line, "f")
    if left_line != right_line:
        reasons.append("line_mismatch")
    if left.settlement_complete is not True or right.settlement_complete is not True:
        reasons.append("incomplete_settlement")
    elif left.settlement_key != right.settlement_key:
        reasons.append("settlement_mismatch")
    left_shape = classify_match_result_shape(left)
    right_shape = classify_match_result_shape(right)
    if left_shape and right_shape and left_shape != right_shape:
        reasons.append("outcome_space_mismatch")
    elif left_shape is None or right_shape is None:
        left_out = {item.casefold() for item in _outcome_space(left)}
        right_out = {item.casefold() for item in _outcome_space(right)}
        if left_out and right_out and left_out != right_out:
            reasons.append("outcome_space_mismatch")
    return reasons


def _count_match_result_facts(
    row: FixtureMarketInventoryRow,
    counts: dict[str, MatchResultVenueCounts],
) -> None:
    for facts in (row.matchbook, row.polymarket, row.kalshi):
        if facts is None:
            continue
        shape = classify_match_result_shape(facts)
        if shape is None:
            continue
        bucket = counts.setdefault(facts.venue.value, MatchResultVenueCounts())
        bucket.total += 1
        if shape == COMPLETE_3WAY:
            bucket.complete_3way_home_draw_away += 1
        elif shape == INCOMPLETE_BINARY:
            bucket.incomplete_or_binary_yes_no += 1
        else:
            bucket.incomplete_other += 1


def _is_grouped_candidate(row: FixtureMarketInventoryRow) -> bool:
    """Inventory row already groups a family/period/line comparison."""

    present = sum(
        1 for facts in (row.matchbook, row.polymarket, row.kalshi) if facts is not None
    )
    if present < 2:
        return False
    return bool(row.family)


def _matcher_reasons(row: FixtureMarketInventoryRow) -> list[str]:
    reasons: list[str] = []
    for item in list(row.match_reasons or []) + list(row.rejection_reasons or []):
        text = str(item or "").strip()
        if not text or text in reasons:
            continue
        if _looks_secret(text):
            continue
        reasons.append(text)
    if row.reason and row.reason not in reasons and not _looks_secret(row.reason):
        reasons.append(row.reason)
    return reasons


def _candidate_view(
    fixture: DiscoveredFixture,
    row: FixtureMarketInventoryRow,
    reasons: list[str],
) -> SafeCandidateView:
    return SafeCandidateView(
        fixture_label=f"{fixture.home_team} vs {fixture.away_team}",
        family=row.family,
        period=row.period,
        line=None if row.line is None else format(row.line, "f"),
        comparison_status=str(row.comparison_status.value if row.comparison_status else None),
        matcher_reasons=reasons,
        venues=[
            _venue_view(facts)
            for facts in (row.matchbook, row.polymarket, row.kalshi)
            if facts is not None
        ],
    )


def _venue_view(facts: VenueMarketFacts) -> SafeVenueMarketView:
    scope = None
    if facts.settlement_key:
        scope = str(facts.settlement_key).split("|", 1)[0] or None
    return SafeVenueMarketView(
        venue=facts.venue.value,
        raw_market_name=_safe_text(facts.raw_market_name),
        raw_market_type=_safe_text(facts.raw_market_type),
        raw_runner_labels=[_safe_text(item) or item for item in facts.raw_runner_labels if item],
        family=facts.family,
        period=facts.period,
        line=None if facts.line is None else format(facts.line, "f"),
        settlement_scope=scope,
        settlement_key=facts.settlement_key,
        settlement_complete=facts.settlement_complete,
        outcome_space=_outcome_space(facts),
        match_result_shape=classify_match_result_shape(facts),
    )


def _outcome_space(facts: VenueMarketFacts) -> list[str]:
    outcomes: list[str] = []
    for quote in facts.best_backs:
        value = str(quote.outcome or "").strip()
        if value and value not in outcomes:
            outcomes.append(value)
    if outcomes:
        return outcomes
    for label in facts.raw_runner_labels:
        text = str(label or "").strip()
        if text and text not in outcomes:
            outcomes.append(text)
    return outcomes


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    if _looks_secret(value):
        return "[redacted]"
    return value


def _looks_secret(value: str) -> bool:
    lowered = value.casefold()
    return any(fragment in lowered for fragment in SECRET_FRAGMENTS)


def _strip_price_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_price_fields(item)
            for key, item in value.items()
            if str(key) not in PRICE_FIELD_NAMES
        }
    if isinstance(value, list):
        return [_strip_price_fields(item) for item in value]
    return value
