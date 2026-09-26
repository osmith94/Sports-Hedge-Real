/** One in-flight live-refresh poll. A slow response does not queue another. */

export type LiveStatusPoll<T> = {
  poll(): Promise<T | null>;
  stop(): void;
};

export function createLiveStatusPoll<T>(options: {
  intervalMs: number;
  fetchStatus: () => Promise<T>;
  onStatus: (status: T) => void;
  onError?: (error: unknown) => void;
  schedule?: (callback: () => void, ms: number) => number;
  cancel?: (handle: number) => void;
}): LiveStatusPoll<T> {
  const schedule = options.schedule ?? ((callback, ms) => window.setInterval(callback, ms));
  const cancel = options.cancel ?? ((handle) => window.clearInterval(handle));
  let stopped = false;
  let inflight: Promise<T | null> | null = null;

  const poll = (): Promise<T | null> => {
    if (stopped) return Promise.resolve(null);
    if (inflight) return inflight;
    const run = (async (): Promise<T | null> => {
      try {
        const status = await options.fetchStatus();
        if (!stopped) options.onStatus(status);
        return stopped ? null : status;
      } catch (error) {
        if (!stopped) options.onError?.(error);
        return null;
      }
    })();
    inflight = run;
    return run.finally(() => {
      if (inflight === run) inflight = null;
    });
  };

  const timer = schedule(() => {
    void poll();
  }, options.intervalMs) as number;
  void poll();

  return {
    poll,
    stop() {
      stopped = true;
      cancel(timer);
    },
  };
}
