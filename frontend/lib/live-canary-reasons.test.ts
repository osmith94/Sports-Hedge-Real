import { describe, expect, it } from "vitest";

import {
  explainLiveCanaryFailure,
  explainLiveCanaryReason,
} from "./live-canary-reasons";

describe("live canary operator reasons", () => {
  it("treats unreachable as unknown, not a location rejection", () => {
    expect(explainLiveCanaryReason("geoblock_unreachable")).toContain(
      "not a location rejection",
    );
    expect(explainLiveCanaryReason("geoblock_unreachable_timeout")).toContain(
      "not a location rejection",
    );
    expect(explainLiveCanaryReason("geoblock_blocked")).toContain("territory rejection");
  });

  it("explains official Matchbook balance without currency as configured-native evidence", () => {
    expect(explainLiveCanaryReason("matchbook_currency_unknown")).toContain(
      "not included in official balance",
    );
    expect(explainLiveCanaryReason("matchbook_currency_mismatch")).toContain(
      "conflicts with the configured native",
    );
  });

  it("prefers backend explanations when present", () => {
    expect(
      explainLiveCanaryFailure(["geoblock_unreachable"], ["custom backend line"]),
    ).toBe("custom backend line");
    expect(explainLiveCanaryFailure([])).toBe("NOT READY");
  });
});
