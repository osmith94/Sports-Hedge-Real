"use client";

import { useEffect, useState } from "react";

import { relativeTime } from "../lib/format";
import {
  OBSERVATION_AGE_TICK_MS,
  startSharedObservationAgeTimer,
} from "../lib/observation-age";

export function useHydratedNowMs(tickMs = OBSERVATION_AGE_TICK_MS): number | null {
  const [nowMs, setNowMs] = useState<number | null>(null);
  useEffect(() => {
    setNowMs(Date.now());
    return startSharedObservationAgeTimer(setNowMs, { tickMs });
  }, [tickMs]);
  return nowMs;
}

export function HydratedRelativeTime({
  iso,
  prefix,
}: {
  iso: string | null | undefined;
  prefix?: string;
}) {
  const nowMs = useHydratedNowMs();
  const label = relativeTime(iso, nowMs);
  return (
    <time dateTime={iso ?? undefined} title={iso ?? undefined}>
      {prefix ? `${prefix} ${label}` : label}
    </time>
  );
}
