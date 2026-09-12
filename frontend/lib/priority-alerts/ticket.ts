import type {
  ManualTicketQuote,
  PriorityAlert,
  ScaledLegCapital,
  VenueCurrencyNeed,
} from "./types";

function roundMoney(value: number): number {
  return Math.round(value * 100) / 100;
}

export function scaleTicket(alert: PriorityAlert, requestedSizeGbp: number): ManualTicketQuote {
  const requested = Number.isFinite(requestedSizeGbp) ? requestedSizeGbp : 0;
  const scale = alert.recommendedSizeGbp > 0 ? requested / alert.recommendedSizeGbp : 0;
  const withinValidatedMaximum = requested > 0 && requested <= alert.maxValidatedSizeGbp + 1e-9;
  const limiting = alert.legs.find((leg) => leg.legId === alert.limitingLegId) ?? alert.legs[0];

  const legs: ScaledLegCapital[] = alert.legs.map((leg) => {
    const pool = alert.autoPools.find((item) => item.venue === leg.venue && item.currency === leg.currency);
    const sourceLeg = alert.legs.find((item) => item.legId === leg.legId);
    const external = sourceLeg?.executionPath === "MANUAL_EXTERNAL";
    const autoPoolNative = external ? 0 : pool?.autoPoolNative ?? 0;
    const stakeNative = roundMoney(leg.recommendedStakeNative * scale);
    const stakeGbp = roundMoney(leg.recommendedStakeGbp * scale);
    const autoCovered = roundMoney(Math.min(stakeNative, autoPoolNative));
    return {
      legId: leg.legId,
      venue: leg.venue,
      currency: leg.currency,
      outcome: leg.outcome,
      selectionLabel: leg.selectionLabel,
      decimalOdds: leg.decimalOdds,
      stakeNative,
      stakeGbp,
      autoPoolNative,
      autoPoolCoveredNative: autoCovered,
      manualOverrideNative: roundMoney(Math.max(0, stakeNative - autoCovered)),
      isLimiting: leg.isLimiting,
      visibleDepthNative: leg.visibleDepthNative,
    };
  });

  const groups = new Map<string, VenueCurrencyNeed>();
  for (const scaled of legs) {
    const source = alert.legs.find((item) => item.legId === scaled.legId);
    const sourceKind = source?.executionPath === "MANUAL_EXTERNAL" ? "MANUAL_EXTERNAL" : "MANUAL_OVERRIDE";
    const key = `${scaled.venue}:${scaled.currency}`;
    const existing = groups.get(key);
    if (existing) {
      existing.requiredNative = roundMoney(existing.requiredNative + scaled.stakeNative);
      existing.autoCoveredNative = roundMoney(Math.min(existing.autoPoolNative, existing.requiredNative));
      existing.additionalManualNative = roundMoney(Math.max(0, existing.requiredNative - existing.autoCoveredNative));
      existing.capitalSource = existing.additionalManualNative > 0 ? sourceKind : "AUTO_POOL";
      continue;
    }
    groups.set(key, {
      venue: scaled.venue,
      currency: scaled.currency,
      requiredNative: scaled.stakeNative,
      autoPoolNative: scaled.autoPoolNative,
      autoCoveredNative: scaled.autoPoolCoveredNative,
      additionalManualNative: scaled.manualOverrideNative,
      capitalSource: scaled.manualOverrideNative > 0 ? sourceKind : "AUTO_POOL",
    });
  }

  const capitalByVenueCurrency = [...groups.values()];
  const totalCapitalGbp = roundMoney(legs.reduce((sum, leg) => sum + leg.stakeGbp, 0));
  const guaranteedProfitGbp = roundMoney(alert.expectedProfitGbpAtRecommended * scale);
  const guaranteedReturnGbp = roundMoney(totalCapitalGbp + guaranteedProfitGbp);
  const additionalManualGbp = roundMoney(
    capitalByVenueCurrency
      .filter((item) => item.currency === "GBP")
      .reduce((sum, item) => sum + item.additionalManualNative, 0),
  );
  const additionalManualUsd = roundMoney(
    capitalByVenueCurrency
      .filter((item) => item.currency === "USD")
      .reduce((sum, item) => sum + item.additionalManualNative, 0),
  );

  return {
    alertId: alert.alertId,
    requestedSizeGbp: roundMoney(requested),
    scale,
    withinValidatedMaximum,
    exceedsValidatedSize: requested > alert.maxValidatedSizeGbp + 1e-9,
    limitingLegLabel: `${limiting.venue} · ${limiting.selectionLabel}`,
    guaranteedReturnGbp,
    guaranteedProfitGbp,
    roi: totalCapitalGbp > 0 ? guaranteedProfitGbp / totalCapitalGbp : 0,
    totalCapitalGbp,
    legs,
    capitalByVenueCurrency,
    additionalManualGbp,
    additionalManualUsd,
  };
}
