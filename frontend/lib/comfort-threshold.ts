export const OPERATOR_COMFORT_THRESHOLDS = [0.005, 0.01, 0.02] as const;

export function distanceToSelectedThresholdPp(
  currentNetEdge: number | null,
  selectedThreshold: number,
): number | null {
  if (currentNetEdge === null || !Number.isFinite(currentNetEdge) || !Number.isFinite(selectedThreshold)) {
    return null;
  }
  return (selectedThreshold - currentNetEdge) * 100;
}

export function uniqueBackendTriggers(triggers: number[]): number[] {
  const seen = new Set<string>();
  const values: number[] = [];
  for (const trigger of triggers) {
    if (!Number.isFinite(trigger)) continue;
    const key = trigger.toFixed(6);
    if (seen.has(key)) continue;
    seen.add(key);
    values.push(trigger);
  }
  return values.sort((left, right) => left - right);
}

export function operatorThresholdOptions(backendTriggers: number[]): number[] {
  const extras = uniqueBackendTriggers(backendTriggers).filter(
    (trigger) => !OPERATOR_COMFORT_THRESHOLDS.some((preset) => Math.abs(preset - trigger) < 1e-9),
  );
  return [...OPERATOR_COMFORT_THRESHOLDS, ...extras];
}

export function grossPricesEffectivelyEqual(grossEdge: number | null): boolean {
  return grossEdge !== null && Math.abs(grossEdge) < 0.0005;
}
