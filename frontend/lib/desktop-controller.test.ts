import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, statSync } from "node:fs";
import { createServer, type Server } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, it } from "vitest";

import { POST as exitRoute } from "../app/api/desktop/exit/route";
import { GET as statusRoute } from "../app/api/desktop/status/route";
import {
  CONTROLLER_NOT_RUNNING_MESSAGE,
  CONTROLLER_PIPE_ENV,
  CONTROLLER_TOKEN_ENV,
  DESKTOP_SESSION_ENV,
  checkExitRequest,
  handleDesktopExit,
  handleDesktopStatus,
  readDesktopControllerConfig,
  sendControllerCommand,
  type SendControllerCommand,
} from "./desktop-controller";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = join(HERE, "..");
const TOKEN = "c".repeat(43);
const ENV = {
  [CONTROLLER_PIPE_ENV]: "\\\\.\\pipe\\sports-hedge-controller-test",
  [CONTROLLER_TOKEN_ENV]: TOKEN,
  [DESKTOP_SESSION_ENV]: "public-session",
  SPORTS_HEDGE_GIT_SHA: "abc123",
};

function exitRequest(headers: Record<string, string> = {}, body: unknown = { confirm: true }): Request {
  return new Request("http://127.0.0.1:3000/api/desktop/exit", {
    method: "POST",
    headers: {
      host: "127.0.0.1:3000",
      origin: "http://127.0.0.1:3000",
      "sec-fetch-site": "same-origin",
      "content-type": "application/json",
      "x-sports-hedge-action": "exit",
      ...headers,
    },
    body: JSON.stringify(body),
  });
}

function recordingSender(reply = { ok: true, result: "accepted", state: "Stopping" }) {
  const calls: string[] = [];
  const send: SendControllerCommand = async (_config, command) => {
    calls.push(command);
    return reply;
  };
  return { calls, send };
}

const savedEnv = { ...process.env };
afterEach(() => {
  for (const key of [CONTROLLER_PIPE_ENV, CONTROLLER_TOKEN_ENV, DESKTOP_SESSION_ENV]) {
    if (savedEnv[key] === undefined) delete process.env[key];
    else process.env[key] = savedEnv[key];
  }
});

describe("desktop controller bridge configuration", () => {
  it("is absent in development / browser-only mode", () => {
    assert.equal(readDesktopControllerConfig({}), null);
    assert.equal(readDesktopControllerConfig({ [CONTROLLER_PIPE_ENV]: "x" }), null);
    assert.equal(readDesktopControllerConfig({ [CONTROLLER_PIPE_ENV]: "x", [CONTROLLER_TOKEN_ENV]: "short" }), null);
    assert.ok(readDesktopControllerConfig(ENV));
  });

  it("never uses NEXT_PUBLIC variables for controller identity or secret", () => {
    const code = readFileSync(join(HERE, "desktop-controller.ts"), "utf8")
      .split("\n")
      .filter((line) => !line.trimStart().startsWith("//"))
      .join("\n");
    assert.doesNotMatch(code, /NEXT_PUBLIC/);
    assert.equal(readDesktopControllerConfig({ NEXT_PUBLIC_SPORTS_HEDGE_CONTROLLER_TOKEN: TOKEN, NEXT_PUBLIC_SPORTS_HEDGE_CONTROLLER_PIPE: "p" }), null);
  });
});

describe("desktop exit route", () => {
  it("fails safely when the desktop controller is not running", async () => {
    const { calls, send } = recordingSender();
    const response = await handleDesktopExit(exitRequest(), {}, send);
    assert.equal(response.status, 503);
    const body = await response.json();
    assert.equal(body.message, CONTROLLER_NOT_RUNNING_MESSAGE);
    assert.deepEqual(calls, []);
  });

  it("the real route handler reports controller missing without env", async () => {
    delete process.env[CONTROLLER_PIPE_ENV];
    delete process.env[CONTROLLER_TOKEN_ENV];
    const response = await exitRoute(exitRequest());
    assert.equal(response.status, 503);
    assert.equal((await response.json()).message, "Desktop controller is not running.");
  });

  it("returns controller-not-running when the pipe cannot be reached", async () => {
    const env = { ...ENV, [CONTROLLER_PIPE_ENV]: join(mkdtempSync(join(tmpdir(), "sh-")), "missing.sock") };
    const response = await handleDesktopExit(exitRequest(), env);
    assert.equal(response.status, 503);
    assert.equal((await response.json()).message, CONTROLLER_NOT_RUNNING_MESSAGE);
  });

  it("rejects cross-site and forged-host requests before contacting the controller", async () => {
    const cases: Array<[Record<string, string>, string]> = [
      [{ origin: "https://evil.example" }, "origin_not_allowed"],
      [{ origin: "http://localhost:5173" }, "origin_not_allowed"],
      [{ origin: "null" }, "origin_not_allowed"],
      [{ host: "evil.example:3000" }, "host_not_allowed"],
      [{ host: "localhost:3000" }, "origin_host_mismatch"],
      [{ "sec-fetch-site": "cross-site" }, "cross_site_request"],
      [{ "sec-fetch-site": "same-site" }, "cross_site_request"],
      [{ "x-sports-hedge-action": "" }, "missing_exit_header"],
      [{ "content-type": "text/plain" }, "json_required"],
    ];
    for (const [headers, error] of cases) {
      const { calls, send } = recordingSender();
      const response = await handleDesktopExit(exitRequest(headers), ENV, send);
      const body = await response.json();
      assert.equal(body.error, error, JSON.stringify(headers));
      assert.ok(response.status === 403 || response.status === 415);
      assert.deepEqual(calls, []);
    }
    const noOrigin = new Request("http://127.0.0.1:3000/api/desktop/exit", {
      method: "POST",
      headers: { host: "127.0.0.1:3000", "content-type": "application/json", "x-sports-hedge-action": "exit" },
      body: JSON.stringify({ confirm: true }),
    });
    assert.equal(checkExitRequest(noOrigin.headers, readDesktopControllerConfig(ENV)!).ok, false);
  });

  it("requires explicit confirmation in the body", async () => {
    const { calls, send } = recordingSender();
    const response = await handleDesktopExit(exitRequest({}, { confirm: false }), ENV, send);
    assert.equal(response.status, 400);
    assert.deepEqual(calls, []);
  });

  it("forwards a confirmed same-origin exit to the controller and never echoes the secret", async () => {
    const { calls, send } = recordingSender();
    const response = await handleDesktopExit(exitRequest(), ENV, send);
    assert.equal(response.status, 202);
    const text = await response.text();
    assert.deepEqual(JSON.parse(text), { ok: true, state: "stopping", result: "accepted" });
    assert.doesNotMatch(text, new RegExp(TOKEN));
    assert.deepEqual(calls, ["shutdown"]);
    const localhost = await handleDesktopExit(exitRequest({ host: "localhost:3000", origin: "http://localhost:3000" }), ENV, send);
    assert.equal(localhost.status, 202);
  });

  it("reports controller rejection honestly", async () => {
    const { send } = recordingSender({ ok: false, result: "unauthorized", state: "Running" });
    const response = await handleDesktopExit(exitRequest(), ENV, send);
    assert.equal(response.status, 502);
  });
});

describe("desktop status route", () => {
  it("reports no desktop controller in development mode", async () => {
    const body = await handleDesktopStatus({}).json();
    assert.equal(body.desktop_controller, false);
    assert.equal(body.mode, "paper");
    assert.equal(body.execution_enabled, false);
  });

  it("never exposes the controller secret or pipe path to the browser", async () => {
    process.env[CONTROLLER_PIPE_ENV] = ENV[CONTROLLER_PIPE_ENV];
    process.env[CONTROLLER_TOKEN_ENV] = TOKEN;
    process.env[DESKTOP_SESSION_ENV] = "public-session";
    const response = statusRoute();
    const text = await response.text();
    const body = JSON.parse(text);
    assert.equal(body.desktop_controller, true);
    assert.equal(body.session_id, "public-session");
    assert.doesNotMatch(text, new RegExp(TOKEN));
    assert.doesNotMatch(text, /pipe/i);
    assert.equal(response.headers.get("cache-control"), "no-store");
  });
});

describe("controller IPC client", () => {
  let server: Server | null = null;
  afterEach(() => {
    server?.close();
    server = null;
  });

  it("sends one newline-delimited authenticated command and parses the reply", async () => {
    const socketPath = join(mkdtempSync(join(tmpdir(), "sh-")), "controller.sock");
    const received: string[] = [];
    server = createServer((socket) => {
      socket.on("data", (chunk) => {
        received.push(chunk.toString("utf8"));
        socket.end(`${JSON.stringify({ ok: true, result: "accepted", state: "Stopping" })}\n`);
      });
    });
    await new Promise<void>((resolve) => server!.listen(socketPath, resolve));
    const reply = await sendControllerCommand({ ...readDesktopControllerConfig(ENV)!, pipePath: socketPath }, "shutdown");
    assert.deepEqual(reply, { ok: true, result: "accepted", state: "Stopping" });
    const message = JSON.parse(received.join("").trim());
    assert.deepEqual(message, { type: "shutdown", token: TOKEN, source: "ui" });
  });
});

describe("browser bundle boundary", () => {
  function walk(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
      const full = join(dir, name);
      return statSync(full).isDirectory() ? walk(full) : [full];
    });
  }

  it("keeps the server-only bridge out of client components", () => {
    const clientFiles = [...walk(join(FRONTEND, "components")), ...walk(join(FRONTEND, "app"))]
      .filter((file) => /\.tsx?$/.test(file) && !/\.test\.tsx?$/.test(file))
      .filter((file) => readFileSync(file, "utf8").trimStart().startsWith('"use client"'));
    assert.ok(clientFiles.length > 0);
    for (const file of clientFiles) {
      const source = readFileSync(file, "utf8");
      assert.doesNotMatch(source, /desktop-controller["']/, file);
      assert.doesNotMatch(source, /SPORTS_HEDGE_CONTROLLER_(TOKEN|PIPE)/, file);
    }
  });
});
