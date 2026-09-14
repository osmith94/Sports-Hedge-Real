import { ApiRequestError, isNotFoundApiError } from "./api";

export function fixtureDetailUnavailableCopy(
  error: unknown,
  canonicalEventId: string,
): string {
  if (isNotFoundApiError(error)) {
    return (
      `Fixture ${canonicalEventId} is not on the latest collection. No demo fixture is ` +
      "substituted. Collect live paper markets, then open the row from the operations console."
    );
  }
  const detail =
    error instanceof ApiRequestError
      ? `HTTP ${error.status}`
      : error instanceof Error && error.message.trim()
        ? error.message.trim()
        : "backend or network error";
  return (
    `Fixture ${canonicalEventId} could not be loaded (${detail}). No demo fixture is ` +
    "substituted."
  );
}
