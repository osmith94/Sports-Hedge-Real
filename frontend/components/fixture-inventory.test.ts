import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const root = dirname(fileURLToPath(import.meta.url));
const workspace = readFileSync(join(root, "fixture-inventory.tsx"), "utf8");
const preview = readFileSync(join(root, "paper-deployment-preview.tsx"), "utf8");

describe("Fixture Detail Market Comparison component contract", () => {
  it("renders operator decision instead of the aggregate mapping badge", () => {
    expect(workspace).toContain("view.decision.label");
    expect(workspace).toContain("decisionBadgeClass");
    expect(workspace).not.toContain("comparisonLabel(row.comparison_status)");
    expect(workspace).not.toContain("Matched equivalent");
  });

  it("shows pair truth, compact venues, and a single operator reason", () => {
    expect(workspace).toContain("view.comparableHeadline");
    expect(workspace).toContain("view.pairBadges");
    expect(workspace).toContain("inventory-venue-card");
    expect(workspace).toContain("view.operatorReason");
    expect(workspace).not.toContain("Reasons:");
    expect(workspace).toContain("Advanced · provenance");
    expect(workspace).toContain("Raw codes:");
  });

  it("links qualifying cards to the existing paper preview without executing from the card", () => {
    expect(workspace).toContain("view.paperAction");
    expect(workspace).toContain("inventory-cta");
    expect(workspace).toContain("inventory-ineligible");
    expect(workspace).not.toContain("simulatePaperFill");
    expect(preview).toContain('id="paper-deployment"');
    expect(preview).toContain('id="bet-ticket"');
    expect(preview).toContain("Confirm paper OPEN");
    expect(preview).toContain("PAPER MODE");
  });
});
