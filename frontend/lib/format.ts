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

export function relativeTime(iso: string | null | undefined, now?: number | null): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "—";
  if (now == null || !Number.isFinite(now)) return iso;
  const deltaSec = Math.round((now - then) / 1000);
  if (Math.abs(deltaSec) < 60) return `${deltaSec}s ago`;
  const deltaMin = Math.round(deltaSec / 60);
  if (Math.abs(deltaMin) < 60) return `${deltaMin}m ago`;
  const deltaHr = Math.round(deltaMin / 60);
  if (Math.abs(deltaHr) < 48) return `${deltaHr}h ago`;
  return new Date(then).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
}

export function kickoffLocalLabel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
}

export function kickoffRelativeLabel(
  iso: string | null | undefined,
  now?: number | null,
): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  if (now == null || !Number.isFinite(now)) return iso;
  const deltaMs = date.getTime() - now;
  const abs = Math.abs(deltaMs);
  const minutes = Math.round(abs / 60_000);
  if (minutes < 1) return deltaMs >= 0 ? "in <1m" : "<1m ago";
  if (minutes < 60) return deltaMs >= 0 ? `in ${minutes}m` : `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return deltaMs >= 0 ? `in ${hours}h` : `${hours}h ago`;
  const days = Math.round(hours / 24);
  return deltaMs >= 0 ? `in ${days}d` : `${days}d ago`;
}
