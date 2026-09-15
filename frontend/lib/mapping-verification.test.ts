import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  canActivateLearnedRule,
  hasSafeReviewCandidate,
  isNativeHundredPercent,
  mappingProvenanceLabel,
  promptContainsSecrets,
  shouldOfferMappingVerify,
  shouldOfferMappingVerifyAction,
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
    expect(page).not.toContain("MappingVerificationPanel");
    expect(page).not.toContain("mapping-verification-panel");
  });
});
