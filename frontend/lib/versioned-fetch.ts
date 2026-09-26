/** Reload a token-addressed read until it succeeds, without overlapping attempts. */

export type VersionedFetch = {
  stop(): void;
};

export function startVersionedFetch<T>(options: {
  load: () => Promise<T>;
  accept: () => boolean;
  onSuccess: (value: T) => void;
  onError?: (error: unknown) => void;
  schedule?: (callback: () => void, ms: number) => number;
  cancel?: (handle: number) => void;
  delayForAttempt?: (attempt: number) => number;
}): VersionedFetch {
  const schedule = options.schedule ?? ((callback, ms) => window.setTimeout(callback, ms));
  const cancel = options.cancel ?? ((handle) => window.clearTimeout(handle));
  const delayForAttempt = options.delayForAttempt ?? ((attempt: number) => Math.min(8000, 1000 * 2 ** (attempt - 1)));
  let stopped = false;
  let inflight = false;
  let timer: number | null = null;
  let attempt = 0;

  const run = () => {
    if (stopped || inflight || !options.accept()) return;
    inflight = true;
    options.load().then(
      (value) => {
        inflight = false;
        if (stopped || !options.accept()) return;
        attempt = 0;
        options.onSuccess(value);
      },
      (error: unknown) => {
        inflight = false;
        if (stopped || !options.accept()) return;
        options.onError?.(error);
        attempt += 1;
        timer = schedule(() => {
          timer = null;
          run();
        }, delayForAttempt(attempt)) as number;
      },
    );
  };

  run();
  return {
    stop() {
      stopped = true;
      if (timer != null) {
        cancel(timer);
        timer = null;
      }
    },
  };
}
