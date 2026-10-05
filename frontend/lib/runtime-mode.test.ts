import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { readModelBadge, runtimeModeFromHealth } from "./runtime-mode";

describe("runtime mode from /health", () => {
  it("keeps paper wording when health says paper and execution is disabled", () => {
    const view = runtimeModeFromHealth({ mode: "paper", execution_enabled: false });
    assert.equal(view.clock, "GBP · PAPER MODE · NO EXECUTION");
    assert.equal(view.subtitle, "paper operations terminal");
    assert.equal(view.footerMode, "PAPER MODE");
    assert.equal(view.simulationSection, false);
    assert.equal(readModelBadge(view, true), "LIVE PAPER READ MODEL");
  });

  it("says real with execution disabled when health says real and execution is false", () => {
    const view = runtimeModeFromHealth({ mode: "real", execution_enabled: false });
    assert.equal(view.clock, "GBP · REAL MODE · EXECUTION DISABLED");
    assert.equal(view.subtitle, "real operations terminal");
    assert.equal(view.footerMode, "REAL MODE");
    assert.equal(view.footerNote, "LIVE ORDER EXECUTION DISABLED");
    assert.match(view.operationsSubtitle, /Live order execution currently disabled/);
    assert.equal(view.simulationSection, true);
    assert.equal(readModelBadge(view, true), "LIVE VENUE READ MODEL");
    assert.doesNotMatch(view.clock, /PAPER/);
  });

  it("does not soften execution enabled", () => {
    const view = runtimeModeFromHealth({ mode: "real", execution_enabled: true });
    assert.equal(view.clock, "GBP · REAL MODE · EXECUTION ENABLED");
    assert.equal(view.footerNote, "LIVE ORDER EXECUTION ENABLED");
    assert.match(view.operationsSubtitle, /Live order execution is enabled/);
  });

  it("fails unknown instead of defaulting to paper", () => {
    for (const health of [null, undefined, {}, { mode: "real" }, { execution_enabled: false }]) {
      const view = runtimeModeFromHealth(health);
      assert.equal(view.known, false);
      assert.equal(view.clock, "GBP · RUNTIME STATUS UNKNOWN");
      assert.equal(view.simulationSection, false);
      assert.doesNotMatch(view.clock, /PAPER MODE/);
    }
  });
});
