import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

describe("paper scan history surface", () => {
  it("loads the latest 100 audit observations and labels that window", () => {
    const page = readFileSync(join(frontendRoot, "app/page.tsx"), "utf8");
    const api = readFileSync(join(frontendRoot, "lib/api.ts"), "utf8");
    assert.match(page, /getPaperScans\("limit=100"\)/);
    assert.match(api, /query = "limit=100"/);
    assert.match(page, /Latest 100 audit observations/);
    assert.match(page, /Not current scanner radar/);
    assert.match(page, /LATEST 100 AUDIT/);
    assert.doesNotMatch(page, /scans\.filter/);
    assert.doesNotMatch(page, /current_radar_rows/);
    assert.doesNotMatch(page, /SCANNER DATA/);
  });
});
