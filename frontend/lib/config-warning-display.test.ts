import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  CONFIG_WARNING_BANNER_CLASS,
  configWarningsImplyOutage,
  isScannerConfigurationWarning,
} from "./config-warning-display";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

describe("operator config warning rendering", () => {
  it("classifies scanner_configuration copy as configuration, not an outage", () => {
    const warning =
      "scanner_configuration: fewer than two venues enabled — arbitrage comparison is not executable (operator venue selection, not a provider outage)";
    assert.equal(isScannerConfigurationWarning(warning), true);
    assert.equal(configWarningsImplyOutage([warning]), false);
    assert.equal(isScannerConfigurationWarning("Matchbook timed out"), false);
  });

  it("discovery panel uses the config banner, not the outage error banner", () => {
    const panel = readFileSync(join(frontendRoot, "components/discovered-fixtures.tsx"), "utf8");
    assert.match(panel, /CONFIG_WARNING_BANNER_CLASS/);
    assert.doesNotMatch(panel, /scan-message scan-message-error/);
    const css = readFileSync(join(frontendRoot, "app/globals.css"), "utf8");
    assert.match(css, /\.scan-message-config/);
    assert.equal(CONFIG_WARNING_BANNER_CLASS.includes("scan-message-config"), true);
  });

  it("paper scanner keeps remaining config warnings off the error alert", () => {
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    assert.match(scan, /CONFIG_WARNING_BANNER_CLASS/);
    assert.match(scan, /state\.report\.config_warnings/);
    assert.match(scan, /scan-message-error/);
  });
});
