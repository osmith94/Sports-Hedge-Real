import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  applyLatestLiveRefresh,
  createLiveRefreshPollGuard,
} from "./live-refresh-poll-guard";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("live-refresh poll generation guard", () => {
  it("drops poll A issued first but returned last after poll B", async () => {
    const guard = createLiveRefreshPollGuard();
    const applied: string[] = [];
    const pollA = deferred<{ id: string }>();
    const pollB = deferred<{ id: string }>();

    const resultA = applyLatestLiveRefresh(guard, () => pollA.promise, (status) => {
      applied.push(status.id);
    });
    const resultB = applyLatestLiveRefresh(guard, () => pollB.promise, (status) => {
      applied.push(status.id);
    });

    pollB.resolve({ id: "B-later-issued" });
    assert.equal(await resultB, true);
    pollA.resolve({ id: "A-earlier-issued" });
    assert.equal(await resultA, false);
    assert.deepEqual(applied, ["B-later-issued"]);
  });

  it("still applies the latest poll when the earlier one fails after a newer success", async () => {
    const guard = createLiveRefreshPollGuard();
    const applied: string[] = [];
    const pollA = deferred<{ id: string }>();
    const pollB = deferred<{ id: string }>();

    const resultA = applyLatestLiveRefresh(guard, () => pollA.promise, (status) => {
      applied.push(status.id);
    });
    const resultB = applyLatestLiveRefresh(guard, () => pollB.promise, (status) => {
      applied.push(status.id);
    });

    pollB.resolve({ id: "B" });
    assert.equal(await resultB, true);
    pollA.reject(new Error("stale network"));
    assert.equal(await resultA, false);
    assert.deepEqual(applied, ["B"]);
  });
});

describe("live-refresh poll surfaces", () => {
  it("shares one live-refresh poll instead of overlapping RunPaperScan and VenueHealthBar polls", () => {
    const scan = readFileSync(join(frontendRoot, "components/run-paper-scan.tsx"), "utf8");
    const bar = readFileSync(join(frontendRoot, "components/venue-health-bar.tsx"), "utf8");
    const provider = readFileSync(join(frontendRoot, "components/live-status-provider.tsx"), "utf8");
    const poll = readFileSync(join(frontendRoot, "lib/live-status-poll.ts"), "utf8");
    const guard = readFileSync(join(frontendRoot, "lib/live-refresh-poll-guard.ts"), "utf8");
    assert.match(guard, /createLiveRefreshPollGuard/);
    assert.match(guard, /isCurrent\(generation/);
    assert.doesNotMatch(scan, /applyLatestLiveRefresh/);
    assert.doesNotMatch(scan, /createLiveRefreshPollGuard/);
    assert.doesNotMatch(scan, /getLiveRefreshStatus/);
    assert.doesNotMatch(bar, /applyLatestLiveRefresh/);
    assert.doesNotMatch(bar, /createLiveRefreshPollGuard/);
    assert.doesNotMatch(bar, /getLiveRefreshStatus/);
    assert.match(scan, /useLiveStatus/);
    assert.match(bar, /useLiveStatus/);
    assert.match(provider, /createLiveStatusPoll/);
    assert.match(poll, /if \(inflight\) return inflight/);
    assert.doesNotMatch(scan, /applyLiveRefresh\(await getLiveRefreshStatus\(\)\)/);
  });
});
