import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import { ApiRequestError, isNotFoundApiError } from "./api";
import { fixtureDetailUnavailableCopy } from "./fixture-detail-error";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

describe("fixture detail error honesty", () => {
  it("treats only HTTP 404 as missing from the current collection", () => {
    const missing = new ApiRequestError("No collected fixture", 404);
    const down = new ApiRequestError("Sports Hedge API request failed (503)", 503);
    const network = new Error("fetch failed");
    assert.equal(isNotFoundApiError(missing), true);
    assert.equal(isNotFoundApiError(down), false);
    assert.equal(isNotFoundApiError(network), false);
    assert.equal(isNotFoundApiError({ status: 404 }), false);
  });

  it("does not call a backend or network failure 'not on the latest collection'", () => {
    const eventId = "evt:pair-decision";
    const notFound = fixtureDetailUnavailableCopy(new ApiRequestError("missing", 404), eventId);
    const unavailable = fixtureDetailUnavailableCopy(
      new ApiRequestError("Sports Hedge API request failed (502)", 502),
      eventId,
    );
    const timeout = fixtureDetailUnavailableCopy(
      new Error("Sports Hedge API request timed out after 15s"),
      eventId,
    );
    assert.match(notFound, /is not on the latest collection/);
    assert.match(notFound, /No demo fixture is substituted/);
    assert.doesNotMatch(unavailable, /is not on the latest collection/);
    assert.match(unavailable, /could not be loaded \(HTTP 502\)/);
    assert.match(unavailable, /No demo fixture is substituted/);
    assert.doesNotMatch(timeout, /is not on the latest collection/);
    assert.match(timeout, /could not be loaded/);
  });

  it("keeps the fixture page on the honest empty copy helper", () => {
    const page = readFileSync(
      join(frontendRoot, "app/arbitrage/fixtures/[eventId]/page.tsx"),
      "utf8",
    );
    const api = readFileSync(join(frontendRoot, "lib/api.ts"), "utf8");
    assert.match(page, /fixtureDetailUnavailableCopy/);
    assert.match(page, /getFixtureDetail/);
    assert.match(api, /class ApiRequestError/);
    assert.match(api, /throw new ApiRequestError/);
    assert.doesNotMatch(page, /demo fixture is substituted into/);
  });
});
