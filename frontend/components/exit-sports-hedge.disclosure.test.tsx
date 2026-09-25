import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, it } from "vitest";

import type { ExitPhase } from "../lib/desktop-exit-state";
import { ExitSportsHedge, ExitSportsHedgeView } from "./exit-sports-hedge";

const HERE = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(HERE, "exit-sports-hedge.tsx"), "utf8");
const sidebar = readFileSync(join(HERE, "sidebar.tsx"), "utf8");

function view(phase: ExitPhase, message: string | null = null): string {
  return renderToStaticMarkup(createElement(ExitSportsHedgeView, { state: { phase, message } }));
}

describe("Exit Sports Hedge control", () => {
  it("renders nothing until the desktop controller is confirmed (dev / browser-only mode)", () => {
    assert.equal(view("checking"), "");
    assert.equal(view("unavailable"), "");
    assert.equal(renderToStaticMarkup(createElement(ExitSportsHedge)), "");
  });

  it("renders a secondary Exit Sports Hedge action in the sidebar footer", () => {
    const html = view("available");
    assert.match(html, /class="desktop-exit-button"/);
    assert.match(html, />Exit Sports Hedge</);
    assert.doesNotMatch(html, /alertdialog/);
    const footer = sidebar.slice(sidebar.indexOf('className="sidebar-footer"'));
    assert.ok(footer.indexOf("<ExitSportsHedge />") > footer.indexOf("PAPER MODE"));
  });

  it("asks for confirmation with the agreed wording", () => {
    const html = view("confirming");
    assert.match(html, /role="alertdialog"/);
    assert.match(html, /Exit Sports Hedge\?/);
    assert.match(html, /This will stop the scanner, backend and local interface\./);
    assert.match(html, />Cancel</);
  });

  it("shows a safe error when the desktop controller is not running", () => {
    const html = view("error", "Desktop controller is not running.");
    assert.match(html, /role="alert"/);
    assert.match(html, /Desktop controller is not running\./);
    assert.doesNotMatch(html, /desktop-exit-confirm/);
  });

  it("renders the stopping and final stopped screens without a server render", () => {
    const stopping = view("stopping");
    assert.match(stopping, /Stopping Sports Hedge/);
    assert.match(stopping, /PAPER MODE/);
    const stopped = view("stopped");
    assert.match(stopped, /Sports Hedge has stopped\./);
    assert.match(stopped, /You may close this browser tab\./);
    assert.doesNotMatch(stopped, /desktop-exit-button/);
  });

  it("never handles the controller secret, closes windows, or reloads the page", () => {
    assert.match(source, /^"use client";/);
    assert.doesNotMatch(source, /SPORTS_HEDGE_CONTROLLER|process\.env|token/i);
    assert.doesNotMatch(source, /window\.close|location\.reload|router\.refresh/);
  });
});
