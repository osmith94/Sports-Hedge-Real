/**
 * Paper-only external-leg confirmation record.
 *
 * Tenet 16: status alone is insufficient. Executed economics and provenance
 * must remain auditable. This is localStorage demo state, not a venue fill.
 */

export const EXTERNAL_LEG_CONFIRMATION_STORAGE_KEY = "sports-hedge.external-leg-confirmation.v1";

export type ExternalLegConfirmationRecord = {
  alertId: string;
  venue: string;
  product: string;
  selection: string;
  executedPrice: number;
  executedSize: number;
  currency: string;
  executedAt: string;
  externalReference: string;
  operatorNote: string;
  recordedAt: string;
  paperOnly: true;
  hedgeRevalidated: false;
};

export type ExternalLegConfirmationDraft = {
  venue: string;
  product: string;
  selection: string;
  executedPrice: string;
  executedSize: string;
  currency: string;
  executedAt: string;
  externalReference: string;
  operatorNote: string;
};

export function parsePositiveNumber(raw: string): number | null {
  const value = Number(raw.trim());
  if (!Number.isFinite(value) || value <= 0) return null;
  return value;
}

export function validateExternalLegConfirmation(
  draft: ExternalLegConfirmationDraft,
): { ok: true; record: Omit<ExternalLegConfirmationRecord, "alertId" | "recordedAt"> } | { ok: false; error: string } {
  const venue = draft.venue.trim();
  const product = draft.product.trim();
  const selection = draft.selection.trim();
  const currency = draft.currency.trim();
  const executedAt = draft.executedAt.trim();
  const externalReference = draft.externalReference.trim();
  const price = parsePositiveNumber(draft.executedPrice);
  const size = parsePositiveNumber(draft.executedSize);

  if (!venue) return { ok: false, error: "Venue / product is required." };
  if (price === null) return { ok: false, error: "Executed price must be a positive number." };
  if (size === null) return { ok: false, error: "Executed size must be a positive number." };
  if (!currency) return { ok: false, error: "Currency is required." };
  if (!executedAt) return { ok: false, error: "Execution timestamp is required." };
  if (!externalReference) return { ok: false, error: "External reference is required." };

  return {
    ok: true,
    record: {
      venue,
      product: product || venue,
      selection,
      executedPrice: price,
      executedSize: size,
      currency,
      executedAt,
      externalReference,
      operatorNote: draft.operatorNote.trim(),
      paperOnly: true,
      hedgeRevalidated: false,
    },
  };
}
