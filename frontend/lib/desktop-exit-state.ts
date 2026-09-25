// Client-safe state for the "Exit Sports Hedge" control. Contains no controller
// secret and no server-only imports.

export const EXIT_LABEL = "Exit Sports Hedge";
export const EXIT_CONFIRM_TITLE = "Exit Sports Hedge?";
export const EXIT_CONFIRM_BODY = "This will stop the scanner, backend and local interface.";
export const STOPPING_TITLE = "Stopping Sports Hedge…";
export const STOPPING_BODY = "Stopping the scanner, backend and local interface.";
export const STOPPED_TITLE = "Sports Hedge has stopped.";
export const STOPPED_BODY = "You may close this browser tab.";
export const STOP_SLOW_TITLE = "Sports Hedge is still stopping.";
export const STOP_SLOW_BODY =
  "The interface is still reachable. Check the Sports Hedge tray icon or logs\\desktop-controller.log.";
export const EXIT_STATUS_PATH = "/api/desktop/status";
export const EXIT_REQUEST_PATH = "/api/desktop/exit";
export const EXIT_HEADER = "x-sports-hedge-action";
export const EXIT_HEADER_VALUE = "exit";
export const CONTROLLER_UNAVAILABLE_MESSAGE = "Desktop controller is not running.";

export type ExitPhase =
  | "checking"
  | "unavailable"
  | "available"
  | "confirming"
  | "requesting"
  | "stopping"
  | "stopped"
  | "stop_slow"
  | "error";

export type ExitState = { phase: ExitPhase; message: string | null };

export type ExitEvent =
  | { type: "status"; desktopController: boolean }
  | { type: "open_confirm" }
  | { type: "cancel" }
  | { type: "confirm" }
  | { type: "accepted" }
  | { type: "failed"; message: string }
  | { type: "server_gone" }
  | { type: "stop_slow" };

export const INITIAL_EXIT_STATE: ExitState = { phase: "checking", message: null };

const TERMINAL: ReadonlySet<ExitPhase> = new Set<ExitPhase>(["stopping", "stopped", "stop_slow"]);

export function exitReducer(state: ExitState, event: ExitEvent): ExitState {
  if (TERMINAL.has(state.phase)) {
    if (event.type === "server_gone") return { phase: "stopped", message: null };
    if (event.type === "stop_slow" && state.phase === "stopping") return { phase: "stop_slow", message: null };
    return state;
  }
  switch (event.type) {
    case "status":
      if (state.phase !== "checking" && state.phase !== "unavailable" && state.phase !== "available") return state;
      return { phase: event.desktopController ? "available" : "unavailable", message: null };
    case "open_confirm":
      return state.phase === "available" || state.phase === "error" ? { phase: "confirming", message: null } : state;
    case "cancel":
      return state.phase === "confirming" || state.phase === "error" ? { phase: "available", message: null } : state;
    case "confirm":
      return state.phase === "confirming" ? { phase: "requesting", message: null } : state;
    case "accepted":
      return state.phase === "requesting" ? { phase: "stopping", message: null } : state;
    case "failed":
      return state.phase === "requesting" ? { phase: "error", message: event.message } : state;
    default:
      return state;
  }
}

export function exitFailureMessage(status: number, body: unknown): string {
  const message =
    body && typeof body === "object" && typeof (body as { message?: unknown }).message === "string"
      ? (body as { message: string }).message
      : null;
  if (status === 503) return message ?? CONTROLLER_UNAVAILABLE_MESSAGE;
  if (message) return message;
  return `Sports Hedge could not start shutdown (HTTP ${status}). Nothing was stopped.`;
}

export function exitRequestInit(): RequestInit {
  return {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: { "content-type": "application/json", [EXIT_HEADER]: EXIT_HEADER_VALUE },
    body: JSON.stringify({ confirm: true }),
  };
}
