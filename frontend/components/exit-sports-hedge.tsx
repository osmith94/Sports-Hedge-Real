"use client";

import { useCallback, useEffect, useReducer, type ReactNode } from "react";
import { createPortal } from "react-dom";

import {
  EXIT_CONFIRM_BODY,
  EXIT_CONFIRM_TITLE,
  EXIT_LABEL,
  EXIT_REQUEST_PATH,
  EXIT_STATUS_PATH,
  INITIAL_EXIT_STATE,
  STOPPED_BODY,
  STOPPED_TITLE,
  STOPPING_BODY,
  STOPPING_TITLE,
  STOP_SLOW_BODY,
  STOP_SLOW_TITLE,
  exitFailureMessage,
  exitReducer,
  exitRequestInit,
  type ExitState,
} from "../lib/desktop-exit-state";

const POLL_INTERVAL_MS = 1000;
const POLL_TIMEOUT_MS = 1500;
const MISSES_BEFORE_STOPPED = 2;
const SLOW_AFTER_MS = 90_000;

type ViewProps = {
  state: ExitState;
  onOpen?: () => void;
  onCancel?: () => void;
  onConfirm?: () => void;
};

// The sidebar is sticky (its own stacking context), so layers covering the
// whole app must be mounted on <body> to sit above the sticky top bar.
function atBodyLevel(layer: ReactNode): ReactNode {
  return typeof document === "undefined" ? layer : createPortal(layer, document.body);
}

export function ExitSportsHedgeView({ state, onOpen, onCancel, onConfirm }: ViewProps) {
  const { phase, message } = state;
  if (phase === "checking" || phase === "unavailable") {
    return null;
  }
  if (phase === "stopping" || phase === "stopped" || phase === "stop_slow") {
    const title = phase === "stopped" ? STOPPED_TITLE : phase === "stop_slow" ? STOP_SLOW_TITLE : STOPPING_TITLE;
    const body = phase === "stopped" ? STOPPED_BODY : phase === "stop_slow" ? STOP_SLOW_BODY : STOPPING_BODY;
    return atBodyLevel(
      <div className="desktop-exit-overlay" role="status" aria-live="polite" data-phase={phase}>
        <div className="desktop-exit-overlay-card">
          <div className="paper-pill"><span className="paper-dot" /> PAPER MODE</div>
          <h1>{title}</h1>
          <p>{body}</p>
        </div>
      </div>,
    );
  }
  return (
    <>
      <button type="button" className="desktop-exit-button" onClick={onOpen} data-phase={phase}>
        {EXIT_LABEL}
      </button>
      {phase === "confirming" || phase === "requesting" || phase === "error" ? atBodyLevel(
        <div className="desktop-exit-backdrop">
          <div className="desktop-exit-dialog" role="alertdialog" aria-modal="true" aria-labelledby="desktop-exit-title">
            <h2 id="desktop-exit-title">{EXIT_CONFIRM_TITLE}</h2>
            <p>{EXIT_CONFIRM_BODY}</p>
            {phase === "error" && message ? <p className="desktop-exit-error" role="alert">{message}</p> : null}
            <div className="desktop-exit-actions">
              <button type="button" className="desktop-exit-cancel" onClick={onCancel} disabled={phase === "requesting"}>
                Cancel
              </button>
              {phase === "error" ? null : (
                <button type="button" className="desktop-exit-confirm" onClick={onConfirm} disabled={phase === "requesting"}>
                  {phase === "requesting" ? "Requesting shutdown…" : EXIT_LABEL}
                </button>
              )}
            </div>
          </div>
        </div>,
      ) : null}
    </>
  );
}

async function statusReachable(): Promise<boolean> {
  try {
    const response = await fetch(EXIT_STATUS_PATH, { cache: "no-store", signal: AbortSignal.timeout(POLL_TIMEOUT_MS) });
    return response.ok;
  } catch {
    return false;
  }
}

export function ExitSportsHedge({ initialState = INITIAL_EXIT_STATE }: { initialState?: ExitState }) {
  const [state, dispatch] = useReducer(exitReducer, initialState);

  useEffect(() => {
    let cancelled = false;
    fetch(EXIT_STATUS_PATH, { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : null))
      .then((body: { desktop_controller?: unknown } | null) => {
        if (!cancelled) dispatch({ type: "status", desktopController: body?.desktop_controller === true });
      })
      .catch(() => {
        if (!cancelled) dispatch({ type: "status", desktopController: false });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const stoppingPhase = state.phase === "stopping" || state.phase === "stop_slow";
  useEffect(() => {
    if (!stoppingPhase) return;
    let cancelled = false;
    let misses = 0;
    const slowTimer = window.setTimeout(() => dispatch({ type: "stop_slow" }), SLOW_AFTER_MS);
    const poll = async () => {
      while (!cancelled) {
        await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
        if (cancelled) return;
        misses = (await statusReachable()) ? 0 : misses + 1;
        if (misses >= MISSES_BEFORE_STOPPED) {
          dispatch({ type: "server_gone" });
          return;
        }
      }
    };
    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(slowTimer);
    };
  }, [stoppingPhase]);

  const confirm = useCallback(async () => {
    dispatch({ type: "confirm" });
    try {
      const response = await fetch(EXIT_REQUEST_PATH, exitRequestInit());
      const body = await response.json().catch(() => null);
      if (response.status === 202 && body?.ok === true) {
        dispatch({ type: "accepted" });
      } else {
        dispatch({ type: "failed", message: exitFailureMessage(response.status, body) });
      }
    } catch {
      dispatch({ type: "failed", message: exitFailureMessage(503, null) });
    }
  }, []);

  return (
    <ExitSportsHedgeView
      state={state}
      onOpen={() => dispatch({ type: "open_confirm" })}
      onCancel={() => dispatch({ type: "cancel" })}
      onConfirm={() => void confirm()}
    />
  );
}
