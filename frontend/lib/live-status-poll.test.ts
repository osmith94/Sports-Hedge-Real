import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { createLiveStatusPoll } from "./live-status-poll";

describe("single-flight live status poll", () => {
  it("keeps one request in flight when the response is slower than the interval", async () => {
    const pending: Array<(value: { n: number }) => void> = [];
    let active = 0;
    let maxActive = 0;
    let calls = 0;
    const applied: number[] = [];
    const harness: { tick: (() => void) | null } = { tick: null };

    const poll = createLiveStatusPoll<{ n: number }>({
      intervalMs: 2000,
      fetchStatus: () => {
        calls += 1;
        active += 1;
        maxActive = Math.max(maxActive, active);
        const id = calls;
        return new Promise<{ n: number }>((resolve) => {
          pending.push((value) => {
            active -= 1;
            resolve(value);
          });
          void id;
        });
      },
      onStatus: (status) => {
        applied.push(status.n);
      },
      schedule: (callback: () => void) => {
        harness.tick = callback;
        return 1;
      },
      cancel: () => undefined,
    });

    assert.equal(calls, 1);
    assert.equal(maxActive, 1);
    harness.tick?.();
    harness.tick?.();
    assert.equal(calls, 1);
    assert.equal(maxActive, 1);
    pending[0]?.({ n: 1 });
    await Promise.resolve();
    await Promise.resolve();
    assert.deepEqual(applied, [1]);
    harness.tick?.();
    assert.equal(calls, 2);
    assert.equal(maxActive, 1);
    poll.stop();
    pending[1]?.({ n: 2 });
    await Promise.resolve();
    assert.deepEqual(applied, [1]);
  });

  it("keeps the last status when a poll fails", async () => {
    let fail = false;
    const applied: string[] = [];
    const errors: unknown[] = [];
    const poll = createLiveStatusPoll({
      intervalMs: 2000,
      fetchStatus: async () => {
        if (fail) throw new Error("down");
        return "ok";
      },
      onStatus: (status) => applied.push(status),
      onError: (error) => errors.push(error),
      schedule: () => 1,
      cancel: () => undefined,
    });
    await Promise.resolve();
    await Promise.resolve();
    assert.deepEqual(applied, ["ok"]);
    fail = true;
    await poll.poll();
    assert.deepEqual(applied, ["ok"]);
    assert.equal(errors.length, 1);
    poll.stop();
  });
});
