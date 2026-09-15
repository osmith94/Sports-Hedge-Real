export const OBSERVATION_AGE_TICK_MS = 1000;

export function parseObservationTimestampMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatObservationAge(
  observedAt: string | null | undefined,
  nowMs: number,
): string {
  const timestampMs = parseObservationTimestampMs(observedAt);
  if (timestampMs === null || !Number.isFinite(nowMs)) return "—";
  const elapsedMs = Math.max(0, nowMs - timestampMs);
  const elapsedSec = Math.floor(elapsedMs / 1000);
  if (elapsedSec < 60) return `${elapsedSec}s`;
  const elapsedMin = Math.floor(elapsedSec / 60);
  if (elapsedMin < 60) return `${elapsedMin}m`;
  const elapsedHours = Math.floor(elapsedMin / 60);
  if (elapsedHours < 24) return `${elapsedHours}h`;
  const elapsedDays = Math.floor(elapsedHours / 24);
  return `${elapsedDays}d`;
}

export function observationTimestampTitle(
  observedAt: string | null | undefined,
  unavailableLabel = "timestamp unavailable",
): string {
  return observedAt && observedAt.trim() ? observedAt : unavailableLabel;
}

type SharedAgeTimerOptions = {
  now?: () => number;
  setInterval?: (handler: () => void, ms: number) => ReturnType<typeof setInterval>;
  clearInterval?: (id: ReturnType<typeof setInterval>) => void;
  tickMs?: number;
};

export function startSharedObservationAgeTimer(
  onTick: (nowMs: number) => void,
  options: SharedAgeTimerOptions = {},
): () => void {
  const now = options.now ?? Date.now;
  const schedule = options.setInterval ?? setInterval;
  const cancel = options.clearInterval ?? clearInterval;
  const tickMs = options.tickMs ?? OBSERVATION_AGE_TICK_MS;
  const id = schedule(() => onTick(now()), tickMs);
  return () => cancel(id);
}
