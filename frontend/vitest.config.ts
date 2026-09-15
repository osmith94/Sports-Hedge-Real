import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["lib/**/*.test.ts", "components/**/*.test.ts"],
    exclude: [
      "lib/tracked-markets-display.test.ts",
      "lib/discovered-fixture-display.test.ts",
      "lib/fixture-detail-error.test.ts",
      "lib/scan-status-display.test.ts",
      "lib/paper-scan-history-display.test.ts",
      "lib/paper-position-management-display.test.ts",
    ],
  },
});
