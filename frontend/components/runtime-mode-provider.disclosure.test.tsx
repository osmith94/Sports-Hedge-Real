import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(HERE, "runtime-mode-provider.tsx"), "utf8");
const api = readFileSync(join(HERE, "../lib/api.ts"), "utf8");

describe("execution confirm is one click, not a typed phrase", () => {
  it("keeps one Confirm EXECUTION ON control and no ARM LIVE CANARY input", () => {
    assert.match(source, /REAL-MONEY EXECUTION/);
    assert.match(source, /Confirm EXECUTION ON/);
    assert.match(source, /does not cancel/);
    assert.match(source, /Max Event/);
    assert.doesNotMatch(source, /ARM LIVE CANARY/);
    assert.doesNotMatch(source, /Confirmation phrase/);
    assert.doesNotMatch(api, /JSON\.stringify\(\{\s*phrase/);
    assert.match(api, /body: "\{\}"/);
  });
});
