// Server-only bridge from the Next.js process to SportsHedge.exe.
// Never import this module from a "use client" component: it reads private
// process environment variables set by the desktop controller for the Next.js
// SERVER process only (never NEXT_PUBLIC_*), and talks to a named pipe.
import { connect } from "node:net";

export const CONTROLLER_PIPE_ENV = "SPORTS_HEDGE_CONTROLLER_PIPE";
export const CONTROLLER_TOKEN_ENV = "SPORTS_HEDGE_CONTROLLER_TOKEN";
export const DESKTOP_SESSION_ENV = "SPORTS_HEDGE_DESKTOP_SESSION_ID";
export const DESKTOP_ORIGINS_ENV = "SPORTS_HEDGE_DESKTOP_ORIGINS";
export const GIT_SHA_ENV = "SPORTS_HEDGE_GIT_SHA";

export const DESKTOP_EXIT_HEADER = "x-sports-hedge-action";
export const DESKTOP_EXIT_HEADER_VALUE = "exit";
export const CONTROLLER_NOT_RUNNING_MESSAGE = "Desktop controller is not running.";
export const DEFAULT_DESKTOP_ORIGINS = ["http://127.0.0.1:3000", "http://localhost:3000"];
const MIN_TOKEN_LENGTH = 32;
const MAX_REPLY_BYTES = 4096;

type Env = Record<string, string | undefined>;

export type DesktopControllerConfig = {
  pipePath: string;
  token: string;
  sessionId: string | null;
  allowedOrigins: string[];
};

export type ControllerReply = {
  ok: boolean;
  result?: string;
  state?: string;
  error?: string;
};

export type ControllerCommand = "shutdown" | "status";

export type SendControllerCommand = (
  config: DesktopControllerConfig,
  command: ControllerCommand,
) => Promise<ControllerReply>;

export function readDesktopControllerConfig(env: Env = process.env): DesktopControllerConfig | null {
  const pipePath = (env[CONTROLLER_PIPE_ENV] ?? "").trim();
  const token = (env[CONTROLLER_TOKEN_ENV] ?? "").trim();
  if (!pipePath || token.length < MIN_TOKEN_LENGTH) {
    return null;
  }
  const origins = (env[DESKTOP_ORIGINS_ENV] ?? "")
    .split(",")
    .map((origin) => origin.trim().toLowerCase())
    .filter(Boolean);
  return {
    pipePath,
    token,
    sessionId: (env[DESKTOP_SESSION_ENV] ?? "").trim() || null,
    allowedOrigins: origins.length > 0 ? origins : DEFAULT_DESKTOP_ORIGINS,
  };
}

export type ExitRequestCheck = { ok: true } | { ok: false; status: number; error: string };

// Browser-origin checks for the exit route. Cross-site pages cannot satisfy
// these: they cannot forge Origin/Host/Sec-Fetch-Site, and the custom header
// forces a CORS preflight that this app never approves.
export function checkExitRequest(headers: Headers, config: DesktopControllerConfig): ExitRequestCheck {
  const allowedHosts = new Set(config.allowedOrigins.map((origin) => safeHost(origin)).filter(Boolean));
  const host = (headers.get("host") ?? "").toLowerCase();
  if (!allowedHosts.has(host)) {
    return { ok: false, status: 403, error: "host_not_allowed" };
  }
  const origin = headers.get("origin");
  if (!origin || !config.allowedOrigins.includes(origin.toLowerCase())) {
    return { ok: false, status: 403, error: "origin_not_allowed" };
  }
  if (safeHost(origin) !== host) {
    return { ok: false, status: 403, error: "origin_host_mismatch" };
  }
  const fetchSite = headers.get("sec-fetch-site");
  if (fetchSite !== null && fetchSite !== "same-origin") {
    return { ok: false, status: 403, error: "cross_site_request" };
  }
  if (headers.get(DESKTOP_EXIT_HEADER) !== DESKTOP_EXIT_HEADER_VALUE) {
    return { ok: false, status: 403, error: "missing_exit_header" };
  }
  const contentType = headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().startsWith("application/json")) {
    return { ok: false, status: 415, error: "json_required" };
  }
  return { ok: true };
}

function safeHost(origin: string): string {
  try {
    return new URL(origin).host.toLowerCase();
  } catch {
    return "";
  }
}

export const sendControllerCommand: SendControllerCommand = (config, command) =>
  new Promise<ControllerReply>((resolve, reject) => {
    const socket = connect(config.pipePath);
    let buffer = "";
    let settled = false;
    const finish = (error: Error | null, reply?: ControllerReply) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      if (error) reject(error);
      else resolve(reply as ControllerReply);
    };
    socket.setTimeout(3000, () => finish(new Error("controller_timeout")));
    socket.on("error", (error) => finish(error));
    socket.on("connect", () => {
      socket.write(`${JSON.stringify({ type: command, token: config.token, source: "ui" })}\n`);
    });
    socket.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      if (buffer.length > MAX_REPLY_BYTES) {
        finish(new Error("controller_reply_too_large"));
        return;
      }
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      try {
        finish(null, JSON.parse(buffer.slice(0, newline)) as ControllerReply);
      } catch {
        finish(new Error("controller_reply_invalid"));
      }
    });
    socket.on("end", () => finish(new Error("controller_closed")));
  });

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

export async function handleDesktopExit(
  request: Request,
  env: Env = process.env,
  send: SendControllerCommand = sendControllerCommand,
): Promise<Response> {
  const config = readDesktopControllerConfig(env);
  if (!config) {
    return json({ ok: false, error: "desktop_controller_not_running", message: CONTROLLER_NOT_RUNNING_MESSAGE }, 503);
  }
  const check = checkExitRequest(request.headers, config);
  if (!check.ok) {
    return json({ ok: false, error: check.error }, check.status);
  }
  let body: unknown = null;
  try {
    body = await request.json();
  } catch {
    body = null;
  }
  if (!body || typeof body !== "object" || (body as { confirm?: unknown }).confirm !== true) {
    return json({ ok: false, error: "confirmation_required" }, 400);
  }
  let reply: ControllerReply;
  try {
    reply = await send(config, "shutdown");
  } catch {
    return json({ ok: false, error: "desktop_controller_not_running", message: CONTROLLER_NOT_RUNNING_MESSAGE }, 503);
  }
  if (!reply.ok) {
    return json({ ok: false, error: "controller_rejected", message: "The desktop controller rejected the exit request." }, 502);
  }
  return json({ ok: true, state: "stopping", result: reply.result ?? "accepted" }, 202);
}

export function handleDesktopStatus(env: Env = process.env): Response {
  const config = readDesktopControllerConfig(env);
  return json(
    {
      desktop_controller: config !== null,
      session_id: config?.sessionId ?? null,
      git_sha: config ? (env[GIT_SHA_ENV] ?? "").trim() || null : null,
      mode: "paper",
      execution_enabled: false,
    },
    200,
  );
}
