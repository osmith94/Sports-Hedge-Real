import { LiveRefreshStatus } from "./api";
import { relativeTime } from "./format";

export type LaneScanCopy = {
  label: string;
  detail: string;
};

function durationLabel(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return `${Math.round(ms / 100) / 10}s`;
}

function nextDueLabel(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "—";
  const delta = Math.max(0, Math.round((then - now) / 1000));
  return `${delta}s`;
}

export function fastScanCopy(
  status: LiveRefreshStatus | null,
  now = Date.now(),
): LaneScanCopy {
  const hot = status?.hot;
  if (!hot) {
    return { label: "Fast scan", detail: "never" };
  }
  if (hot.cycle_in_progress) {
    return { label: "Fast scan", detail: "in progress" };
  }
  const when = hot.last_completed_at
    ? relativeTime(hot.last_completed_at, now)
    : "never";
  const leftover = hot.not_evaluated_count
    ? ` · partial (${hot.not_evaluated_count} not evaluated)`
    : "";
  const persist =
    hot.persist_ok === false || hot.last_persist_error
      ? " · persist/auto-capture failed"
      : "";
  return {
    label: "Fast scan",
    detail: `${when} · ${durationLabel(hot.last_duration_ms)} · next ${nextDueLabel(hot.next_due_at, now)} · ${hot.fixture_count} hot${leftover}${persist}`,
  };
}

export function fullSweepCopy(
  status: LiveRefreshStatus | null,
  now = Date.now(),
): LaneScanCopy {
  const universe = status?.universe;
  const hot = status?.hot;
  if (!universe) {
    return { label: "Full sweep", detail: "never" };
  }
  if (universe.cycle_in_progress) {
    return { label: "Full sweep", detail: "chunk in progress" };
  }
  const work = universe.generation_work_used_s ?? 0;
  const budget = universe.generation_budget_seconds ?? 150;
  const leftover = universe.not_evaluated_count ?? 0;
  const evaluated = universe.evaluated_count ?? 0;
  const persist =
    universe.persist_ok === false || universe.last_persist_error
      ? " · persist/auto-capture failed"
      : "";
  return {
    label: "Full sweep",
    detail: `chunk ${durationLabel(universe.chunk_last_duration_ms ?? universe.last_duration_ms)} · gen ${Math.round(work)}/${Math.round(budget)}s · next HOT in ${nextDueLabel(hot?.next_due_at, now)} · ${universe.fixture_count} universe · ${evaluated} evaluated / ${leftover} not evaluated${persist}`,
  };
}

export function dualScanStatusLines(
  status: LiveRefreshStatus | null,
  now = Date.now(),
): string[] {
  const fast = fastScanCopy(status, now);
  const full = fullSweepCopy(status, now);
  return [`${fast.label} · ${fast.detail}`, `${full.label} · ${full.detail}`];
}
