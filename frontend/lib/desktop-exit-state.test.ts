import assert from "node:assert/strict";
import { describe, it } from "vitest";

import {
  CONTROLLER_UNAVAILABLE_MESSAGE,
  INITIAL_EXIT_STATE,
  exitFailureMessage,
  exitReducer,
  exitRequestInit,
  type ExitEvent,
  type ExitState,
} from "./desktop-exit-state";

function run(events: ExitEvent[], start: ExitState = INITIAL_EXIT_STATE): ExitState {
  return events.reduce(exitReducer, start);
}

describe("exit control state", () => {
  it("stays hidden unless the server reports a desktop controller", () => {
    assert.equal(run([{ type: "status", desktopController: false }]).phase, "unavailable");
    assert.equal(run([{ type: "status", desktopController: false }, { type: "open_confirm" }]).phase, "unavailable");
    assert.equal(run([{ type: "status", desktopController: true }]).phase, "available");
  });

  it("requires confirmation before any request", () => {
    const available = run([{ type: "status", desktopController: true }]);
    assert.equal(exitReducer(available, { type: "confirm" }).phase, "available");
    const confirming = exitReducer(available, { type: "open_confirm" });
    assert.equal(confirming.phase, "confirming");
    assert.equal(exitReducer(confirming, { type: "cancel" }).phase, "available");
    assert.equal(exitReducer(confirming, { type: "confirm" }).phase, "requesting");
  });

  it("enters stopping then stopped entirely client-side after acceptance", () => {
    const stopping = run([
      { type: "status", desktopController: true },
      { type: "open_confirm" },
      { type: "confirm" },
      { type: "accepted" },
    ]);
    assert.equal(stopping.phase, "stopping");
    assert.equal(exitReducer(stopping, { type: "status", desktopController: false }).phase, "stopping");
    assert.equal(exitReducer(stopping, { type: "open_confirm" }).phase, "stopping");
    const slow = exitReducer(stopping, { type: "stop_slow" });
    assert.equal(slow.phase, "stop_slow");
    assert.equal(exitReducer(slow, { type: "server_gone" }).phase, "stopped");
    const stopped = exitReducer(stopping, { type: "server_gone" });
    assert.equal(stopped.phase, "stopped");
    assert.equal(exitReducer(stopped, { type: "failed", message: "x" }).phase, "stopped");
  });

  it("shows a safe error when the desktop controller is missing", () => {
    const failed = run([
      { type: "status", desktopController: true },
      { type: "open_confirm" },
      { type: "confirm" },
      { type: "failed", message: exitFailureMessage(503, null) },
    ]);
    assert.equal(failed.phase, "error");
    assert.equal(failed.message, CONTROLLER_UNAVAILABLE_MESSAGE);
    assert.equal(exitReducer(failed, { type: "cancel" }).phase, "available");
    assert.equal(exitFailureMessage(403, { ok: false, error: "origin_not_allowed" }), "Sports Hedge could not start shutdown (HTTP 403). Nothing was stopped.");
  });

  it("sends only a same-origin confirmation body with the custom exit header", () => {
    const init = exitRequestInit();
    assert.equal(init.method, "POST");
    assert.equal(init.credentials, "same-origin");
    assert.deepEqual(JSON.parse(String(init.body)), { confirm: true });
    const headers = init.headers as Record<string, string>;
    assert.equal(headers["x-sports-hedge-action"], "exit");
    assert.doesNotMatch(JSON.stringify(init), /token/i);
  });
});
