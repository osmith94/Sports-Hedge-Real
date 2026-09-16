export type LiveRefreshPollGuard = {
  begin(): number;
  isCurrent(generation: number): boolean;
};

export function createLiveRefreshPollGuard(): LiveRefreshPollGuard {
  let latest = 0;
  return {
    begin() {
      latest += 1;
      return latest;
    },
    isCurrent(generation: number) {
      return generation === latest;
    },
  };
}

export async function applyLatestLiveRefresh<T>(
  guard: LiveRefreshPollGuard,
  fetchStatus: () => Promise<T>,
  apply: (status: T) => void,
): Promise<boolean> {
  const generation = guard.begin();
  let status: T;
  try {
    status = await fetchStatus();
  } catch (error) {
    if (!guard.isCurrent(generation)) return false;
    throw error;
  }
  if (!guard.isCurrent(generation)) return false;
  apply(status);
  return true;
}
