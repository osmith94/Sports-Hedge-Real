export function number(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function percent(value: string | number | null | undefined, digits = 2): string {
  const parsed = number(value);
  return parsed === null ? "—" : `${(parsed * 100).toFixed(digits)}%`;
}

export function percentPoints(value: string | number | null | undefined, digits = 2): string {
  const parsed = number(value);
  return parsed === null ? "—" : `${parsed.toFixed(digits)}pp`;
}

export function money(
  value: string | number | null | undefined,
  currency: "GBP" | "USD" = "GBP",
): string {
  const parsed = number(value);
  return parsed === null
    ? "—"
    : new Intl.NumberFormat("en-GB", { style: "currency", currency }).format(parsed);
}

export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "—";
  const deltaSec = Math.round((now - then) / 1000);
  if (Math.abs(deltaSec) < 60) return `${deltaSec}s ago`;
  const deltaMin = Math.round(deltaSec / 60);
  if (Math.abs(deltaMin) < 60) return `${deltaMin}m ago`;
  const deltaHr = Math.round(deltaMin / 60);
  if (Math.abs(deltaHr) < 48) return `${deltaHr}h ago`;
  return new Date(then).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
}
