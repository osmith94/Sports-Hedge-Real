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

export function hasSafeReviewCandidate(
  candidate?: MappingReviewCandidate | null,
): boolean {
  return Boolean(candidate && Array.isArray(candidate.sides) && candidate.sides.length >= 2);
}

export function shouldOfferMappingVerifyAction(
  confidence: number | null | undefined,
  candidate?: MappingReviewCandidate | null,
): boolean {
  return shouldOfferMappingVerify(confidence) && hasSafeReviewCandidate(candidate);
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

/** Declared conflicts that may independently block learned-rule activation. */
export const STRUCTURAL_BLOCKER_FIELDS = new Set([
  "sport",
  "competition",
  "kickoff",
  "market_family",
  "family",
  "period",
  "line",
  "settlement",
  "settlement_key",
  "settlement_scope",
  "outcome_space",
  "outcome_model",
]);

const STRUCTURAL_BLOCKER_ALIASES: Record<string, string> = {
  family: "market_family",
  settlement_key: "settlement",
  outcome_model: "outcome_space",
};

export function canonicalStructuralBlocker(field: string): string | null {
  const key = field.trim().toLowerCase();
  if (!key || !STRUCTURAL_BLOCKER_FIELDS.has(key)) return null;
  return STRUCTURAL_BLOCKER_ALIASES[key] ?? key;
}

export function structuralConflictsFromDeclared(fields?: string[] | null): string[] {
  if (!fields) return [];
  return [...new Set(fields.flatMap((field) => {
    const canonical = canonicalStructuralBlocker(field);
    return canonical ? [canonical] : [];
  }))].sort();
}

export function canActivateLearnedRule(input: {
  verdict: MappingVerdict | null;
  operatorConfirmed: boolean;
  conflictingFields?: string[] | null;
  activationBlockedReason?: string | null;
}): boolean {
  if (input.verdict !== "verified" || input.operatorConfirmed !== true) return false;
  if (input.activationBlockedReason) return false;
  return structuralConflictsFromDeclared(input.conflictingFields).length === 0;
}
