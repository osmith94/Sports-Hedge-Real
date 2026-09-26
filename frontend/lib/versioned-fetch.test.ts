import assert from "node:assert/strict";
import { describe, it } from "vitest";

import { startVersionedFetch } from "./versioned-fetch";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("versioned resource fetch", () => {
  it("retries one failed read later and does not start a second request while one is in flight", async () => {
    const pending = deferred<string>();
    let calls = 0;
    const scheduled: Array<() => void> = [];
    let loaded = "";
    const task = startVersionedFetch({
      load: () => {
        calls += 1;
        return calls === 1 ? pending.promise : Promise.resolve("ok");
      },
      accept: () => loaded !== "v1",
      onSuccess: (value) => {
        loaded = value === "ok" ? "v1" : value;
      },
      schedule: (callback) => {
        scheduled.push(callback);
        return scheduled.length;
      },
      cancel: () => undefined,
      delayForAttempt: () => 1000,
    });

    assert.equal(calls, 1);
    pending.reject(new Error("down"));
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(scheduled.length, 1);
    assert.equal(calls, 1);
    scheduled[0]?.();
    await Promise.resolve();
    assert.equal(calls, 2);
    assert.equal(loaded, "v1");
    task.stop();
  });

  it("does not apply or retry after stop", async () => {
    const pending = deferred<string>();
    let applied = 0;
    let errors = 0;
    const task = startVersionedFetch({
      load: () => pending.promise,
      accept: () => true,
      onSuccess: () => {
        applied += 1;
      },
      onError: () => {
        errors += 1;
      },
      schedule: () => 1,
      cancel: () => undefined,
    });
    task.stop();
    pending.resolve("late");
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(applied, 0);
    pending.reject(new Error("late"));
    await Promise.resolve();
    assert.equal(errors, 0);
  });
});
