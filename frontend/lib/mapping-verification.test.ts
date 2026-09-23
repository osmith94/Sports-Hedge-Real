import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  canActivateLearnedRule,
  fingerprintOpportunityVerifyRow,
  hasSafeReviewCandidate,
  isNativeHundredPercent,
  mappingConfirmRequest,
  mappingProvenanceLabel,
  mappingVerifyConfirmDecision,
  mappingVerifyEvidenceFingerprint,
  promptContainsSecrets,
  shouldOfferMappingVerify,
  shouldOfferMappingVerifyAction,
  snapshotMappingCandidate,
  structuralActivationBlockedReason,
  verifySessionMatchesCurrent,
} from "./mapping-verification";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

describe("mapping verification seam", () => {
  it("offers Verify only when confidence is below 100%", () => {
    expect(shouldOfferMappingVerify(0.96)).toBe(true);
    expect(shouldOfferMappingVerify(1)).toBe(false);
    expect(shouldOfferMappingVerify(0)).toBe(true);
  });

  it("does not offer Verify without a safe current review candidate", () => {
    expect(hasSafeReviewCandidate(null)).toBe(false);
    expect(shouldOfferMappingVerifyAction(0.96, null)).toBe(false);
    expect(
      shouldOfferMappingVerifyAction(0.96, {
        sides: [
          {
            venue: "matchbook",
            source_event_id: "a",
            source_market_id: "m1",
            raw_home_team: "Leeds",
            raw_away_team: "Chelsea",
            raw_competition: "PL",
            kickoff_utc: "2026-09-20T15:00:00Z",
          },
          {
            venue: "polymarket",
            source_event_id: "b",
            source_market_id: "m2",
            raw_home_team: "Leeds United FC",
            raw_away_team: "Chelsea FC",
            raw_competition: "PL",
            kickoff_utc: "2026-09-20T15:00:00Z",
          },
        ],
      }),
    ).toBe(true);
  });

  it("does not require Verify on a 100% native mapping", () => {
    expect(
      isNativeHundredPercent(1, { mapping_source: "native_deterministic" }),
    ).toBe(true);
    expect(
      shouldOfferMappingVerify(1),
    ).toBe(false);
    expect(
      isNativeHundredPercent(1, {
        mapping_source: "operator_verified",
        rule_id: "maprule:abc",
        rule_version: 2,
      }),
    ).toBe(false);
    expect(
      mappingProvenanceLabel({
        mapping_source: "operator_verified",
        rule_id: "maprule:abc",
        rule_version: 2,
      }),
    ).toBe("operator_verified / learned alias maprule:abc v2");
  });

  it("refuses to treat ChatGPT text as activation without explicit confirmation", () => {
    expect(
      canActivateLearnedRule({ verdict: "verified", operatorConfirmed: false }),
    ).toBe(false);
    expect(
      canActivateLearnedRule({ verdict: "ambiguous", operatorConfirmed: true }),
    ).toBe(false);
    expect(
      canActivateLearnedRule({ verdict: "not_verified", operatorConfirmed: true }),
    ).toBe(false);
    expect(
      canActivateLearnedRule({ verdict: "verified", operatorConfirmed: true }),
    ).toBe(true);
    expect(
      canActivateLearnedRule({
        verdict: "verified",
        operatorConfirmed: true,
        conflictingFields: ["settlement"],
      }),
    ).toBe(false);
    expect(
      canActivateLearnedRule({
        verdict: "verified",
        operatorConfirmed: true,
        conflictingFields: ["market_family", "period", "outcome_space"],
      }),
    ).toBe(false);
    expect(
      canActivateLearnedRule({
        verdict: "verified",
        operatorConfirmed: true,
        conflictingFields: ["home_team", "raw_event_name"],
      }),
    ).toBe(true);
    expect(
      canActivateLearnedRule({
        verdict: "verified",
        operatorConfirmed: true,
        conflictingFields: ["home_team"],
        activationBlockedReason: "structural_conflicts:settlement",
      }),
    ).toBe(false);
  });

  it("keeps VERIFIED interpret confirmable after explicit_operator_confirmation_required", () => {
    const interpreted = {
      verdict: "verified" as const,
      activationBlockedReason: "explicit_operator_confirmation_required",
      reviewId: "maprev:interpret-1",
    };
    expect(structuralActivationBlockedReason(interpreted.activationBlockedReason)).toBeNull();
    expect(
      canActivateLearnedRule({
        verdict: interpreted.verdict,
        operatorConfirmed: false,
        activationBlockedReason: interpreted.activationBlockedReason,
      }),
    ).toBe(false);
    expect(
      canActivateLearnedRule({
        verdict: interpreted.verdict,
        operatorConfirmed: true,
        activationBlockedReason: interpreted.activationBlockedReason,
      }),
    ).toBe(true);
    expect(
      canActivateLearnedRule({
        verdict: "verified",
        operatorConfirmed: true,
        activationBlockedReason: "structural_conflicts:settlement",
      }),
    ).toBe(false);
    const candidate = {
      sides: [
        {
          venue: "matchbook" as const,
          source_event_id: "a",
          source_market_id: "m1",
          raw_home_team: "Leeds",
          raw_away_team: "Chelsea",
          raw_competition: "PL",
          kickoff_utc: "2026-09-20T15:00:00Z",
        },
        {
          venue: "polymarket" as const,
          source_event_id: "b",
          source_market_id: "m2",
          raw_home_team: "Leeds United FC",
          raw_away_team: "Chelsea FC",
          raw_competition: "PL",
          kickoff_utc: "2026-09-20T15:00:00Z",
        },
      ],
    };
    const confirmBody = mappingConfirmRequest({
      candidate,
      chatgptText: "VERIFIED. Strip FC suffix.",
      verdict: interpreted.verdict,
      operatorConfirmed: true,
      reviewId: interpreted.reviewId,
    });
    expect(confirmBody.review_id).toBe("maprev:interpret-1");
    expect(confirmBody.operator_confirmed).toBe(true);
    expect(confirmBody.manual_verdict).toBe("verified");
  });

  it("binds Verify to exact evidence and fails closed when the same opportunity refreshes", () => {
    const candidateA = {
      sides: [
        {
          venue: "matchbook" as const,
          source_event_id: "a",
          source_market_id: "m1",
          raw_home_team: "Leeds",
          raw_away_team: "Chelsea",
          raw_competition: "PL",
          kickoff_utc: "2026-09-20T15:00:00Z",
        },
        {
          venue: "polymarket" as const,
          source_event_id: "b",
          source_market_id: "m2",
          raw_home_team: "Leeds United FC",
          raw_away_team: "Chelsea FC",
          raw_competition: "PL",
          kickoff_utc: "2026-09-20T15:00:00Z",
        },
      ],
      current_confidence: 0.96,
    };
    const candidateB = {
      ...candidateA,
      current_confidence: 0.91,
      sides: [
        candidateA.sides[0],
        {
          ...candidateA.sides[1],
          raw_home_team: "Leeds United",
          source_event_id: "pm-leeds-b",
        },
      ],
    };
    const rowA = {
      id: "opp-1",
      observedAt: "2026-09-15T12:00:00.000Z",
      mappingConfidence: 0.96,
      mappingCandidate: candidateA,
    };
    const rowB = {
      id: "opp-1",
      observedAt: "2026-09-15T12:00:45.000Z",
      mappingConfidence: 0.91,
      mappingCandidate: candidateB,
    };
    const fingerprintA = fingerprintOpportunityVerifyRow(rowA);
    const fingerprintB = fingerprintOpportunityVerifyRow(rowB);
    expect(fingerprintA).not.toBe(fingerprintB);
    expect(verifySessionMatchesCurrent(fingerprintA, fingerprintB)).toBe(false);
    const interpreted = mappingVerifyConfirmDecision({
      sessionFingerprint: fingerprintA,
      currentFingerprint: fingerprintA,
      sessionCandidate: snapshotMappingCandidate(candidateA),
      chatgptText: "VERIFIED. Strip FC suffix.",
      verdict: "verified",
      reviewId: "maprev:a",
    });
    expect(interpreted.ok).toBe(true);
    if (interpreted.ok) {
      expect(interpreted.request.review_id).toBe("maprev:a");
      expect(interpreted.request.candidate.sides[1].source_event_id).toBe("b");
    }
    const refreshed = mappingVerifyConfirmDecision({
      sessionFingerprint: fingerprintA,
      currentFingerprint: fingerprintB,
      sessionCandidate: candidateA,
      chatgptText: "VERIFIED. Strip FC suffix.",
      verdict: "verified",
      reviewId: "maprev:a",
    });
    expect(refreshed.ok).toBe(false);
    if (!refreshed.ok) {
      expect(refreshed.reason).toBe("mapping_verify_evidence_changed");
    }
    const freshB = mappingVerifyConfirmDecision({
      sessionFingerprint: fingerprintB,
      currentFingerprint: fingerprintB,
      sessionCandidate: snapshotMappingCandidate(candidateB),
      chatgptText: "VERIFIED. Strip FC suffix.",
      verdict: "verified",
      reviewId: "maprev:b",
    });
    expect(freshB.ok).toBe(true);
    if (freshB.ok) {
      expect(freshB.request.review_id).toBe("maprev:b");
      expect(freshB.request.candidate.sides[1].source_event_id).toBe("pm-leeds-b");
      expect(freshB.request.review_id).not.toBe("maprev:a");
    }
    expect(
      mappingVerifyEvidenceFingerprint({
        opportunityId: "opp-1",
        observedAt: rowA.observedAt,
        mappingConfidence: 0.96,
        candidate: candidateA,
      }),
    ).toBe(fingerprintA);
  });

  it("treats credential-like fragments as secrets that must not appear in prompts", () => {
    expect(promptContainsSecrets("Are these two venue markets the same?")).toBe(false);
    expect(promptContainsSecrets("Authorization: Bearer super-secret")).toBe(true);
    expect(promptContainsSecrets("matchbook password=demo")).toBe(true);
  });

  it("keeps the verification panel reusable and wires it through Opportunity Monitor, not page.tsx", () => {
    const panel = readFileSync(
      join(frontendRoot, "components/mapping-verification-panel.tsx"),
      "utf8",
    );
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const monitor = readFileSync(join(frontendRoot, "components/opportunity-monitor.tsx"), "utf8");
    expect(panel).toContain("Copy verification prompt");
    expect(monitor).toContain("MappingVerificationPanel");
    expect(monitor).toContain("buildMappingReviewPrompt");
    expect(monitor).toContain("interpretMappingReview");
    expect(monitor).toContain("confirmMappingReview");
    expect(monitor).toContain("mappingVerifyConfirmDecision");
    expect(monitor).toContain("fingerprintOpportunityVerifyRow");
    expect(monitor).toContain("snapshotMappingCandidate");
    expect(monitor).toContain("session.candidate");
    expect(monitor).toContain("failClosedEvidenceChanged");
    expect(page).not.toContain("MappingVerificationPanel");
    expect(page).not.toContain("mapping-verification-panel");
  });
});
