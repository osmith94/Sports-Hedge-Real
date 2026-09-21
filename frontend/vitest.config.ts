import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["lib/**/*.test.ts", "components/**/*.test.ts"],
    // node:test display suites run via `tsx --test` in npm test, not Vitest.
    exclude: [
      "lib/tracked-markets-display.test.ts",
      "lib/discovered-fixture-display.test.ts",
      "lib/fixture-detail-error.test.ts",
      "lib/scan-status-display.test.ts",
      "lib/paper-scan-history-display.test.ts",
      "lib/paper-position-management-display.test.ts",
      "lib/paper-trade-display.test.ts",
      "lib/opportunity-monitor-display.test.ts",
      "lib/venue-health-display.test.ts",
      "lib/config-warning-display.test.ts",
      "lib/venue-participation-display.test.ts",
      "lib/live-refresh-poll-guard.test.ts",
      "lib/hot-fixture-roster-display.test.ts",
      "lib/scan-cycle-history-display.test.ts",
      "lib/catalogue-coverage-display.test.ts",
      "lib/venue-degradation-incident.test.ts",
      "lib/system-load-display.test.ts",
      "lib/active-trade-timeline-display.test.ts",
      "lib/activity-feed-display.test.ts",
      "lib/opportunity-history-display.test.ts",
    ],
  },
});
