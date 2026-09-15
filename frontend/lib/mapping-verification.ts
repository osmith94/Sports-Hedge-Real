import { Venue } from "./api";

export type MappingProvenanceSource = "native_deterministic" | "operator_verified";

export type MappingRuleType =
  | "venue_suffix_strip"
  | "venue_prefix_strip"
  | "venue_name_convention"
  | "venue_market_label_convention"
  | "fixture_specific_alias";

export type MappingVerdict = "verified" | "not_verified" | "ambiguous";

export type MappingProvenance = {
  mapping_source?: MappingProvenanceSource | null;
  rule_id?: string | null;
  rule_version?: number | null;
  rule_type?: MappingRuleType | null;
  applied_rule_ids?: string[];
};

export type MappingSideEvidence = {
  venue: Venue;
  source_event_id: string;
  source_market_id: string;
  raw_event_name?: string | null;
  raw_home_team: string;
  raw_away_team: string;
  raw_competition: string;
  kickoff_utc: string;
  raw_market_name?: string | null;
  raw_market_type?: string | null;
  raw_runner_labels?: string[];
  family?: string | null;
  period?: string | null;
  line?: string | null;
  settlement_key?: string | null;
  settlement_scope?: string | null;
  outcome_space?: string[];
  current_canonical_candidate?: string | null;
  confidence?: number | null;
  match_reasons?: string[];
};

export type MappingReviewCandidate = {
  sides: MappingSideEvidence[];
  current_confidence?: number | null;
  current_reasons?: string[];
  current_matched?: boolean | null;
  conflicting_fields?: string[];
};

export type MappingProposedRule = {
  rule_id: string;
  version: number;
  enabled: boolean;
  revoked: boolean;
  source: string;
  rule_type: MappingRuleType;
  venue: Venue;
  field_scope: string;
  raw_pattern: string;
  canonical_transformation: string;
  guardrails: Record<string, unknown>;
  evidence: string;
};

/** Verify is required only when mapping confidence is below 100%. */
export function shouldOfferMappingVerify(confidence: number | null | undefined): boolean {
  if (confidence == null || !Number.isFinite(confidence)) return false;
  return confidence < 1;
}

export function isNativeHundredPercent(
  confidence: number | null | undefined,
  provenance?: MappingProvenance | null,
): boolean {
  if (confidence !== 1) return false;
  const source = provenance?.mapping_source ?? "native_deterministic";
  return source === "native_deterministic";
}

export function mappingProvenanceLabel(provenance?: MappingProvenance | null): string {
  if (provenance?.mapping_source === "operator_verified") {
    const id = provenance.rule_id ? ` ${provenance.rule_id}` : "";
    const version =
      provenance.rule_version != null ? ` v${provenance.rule_version}` : "";
    return `operator_verified / learned alias${id}${version}`;
  }
  return "native deterministic / provider mapping";
}

export function mappingConfidencePercent(confidence: number): string {
  return `${(confidence * 100).toFixed(1)}%`;
}

export function promptContainsSecrets(prompt: string): boolean {
  const lowered = prompt.toLowerCase();
  return ["password", "api_key", "authorization", "bearer ", "secret", "token="].some(
    (fragment) => lowered.includes(fragment),
  );
}

export function canActivateLearnedRule(input: {
  verdict: MappingVerdict | null;
  operatorConfirmed: boolean;
}): boolean {
  return input.verdict === "verified" && input.operatorConfirmed === true;
}
