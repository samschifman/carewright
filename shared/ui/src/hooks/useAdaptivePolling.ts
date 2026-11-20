import { useCallback, useEffect, useRef, useState } from "react";

export interface UseAdaptivePollingOptions<T> {
  fetcher: () => Promise<T>;
  isComplete?: (data: T) => boolean;
  shouldRetry?: (error: Error) => boolean;
  initialInterval?: number;
  slowInterval?: number;
  slowAfterMs?: number;
  idleInterval?: number;
  idleAfterMs?: number;
  enabled?: boolean;
}

export interface UseAdaptivePollingResult<T> {
  data: T | null;
  isLoading: boolean;
  error: Error | null;
  lastChanged: Date | null;
}

export function useAdaptivePolling<T>({
  fetcher,
  isComplete,
  shouldRetry,
  initialInterval = 2000,
  slowInterval = 10000,
  slowAfterMs = 30000,
  idleInterval = 30000,
  idleAfterMs = 120000,
  enabled = true,
}: UseAdaptivePollingOptions<T>): UseAdaptivePollingResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [lastChanged, setLastChanged] = useState<Date | null>(null);

  const lastDataRef = useRef<string>("");
  const startTimeRef = useRef<number>(Date.now());
  const completedRef = useRef(false);

  // isComplete is a predicate (behavior), not a lifecycle trigger. Keeping it in
  // a ref — rather than in poll's deps — means callers can pass it inline (e.g.
  // `() => true`) without a new identity restarting the poll effect on every
  // render, which otherwise causes an infinite fetch loop (#125). fetcher stays
  // a dependency: changing it (new id, refresh) intentionally restarts polling.
  const isCompleteRef = useRef(isComplete);
  isCompleteRef.current = isComplete;
  const shouldRetryRef = useRef(shouldRetry);
  shouldRetryRef.current = shouldRetry;

  const poll = useCallback(async () => {
    if (completedRef.current) return;
    try {
      setIsLoading(true);
      const result = await fetcher();
      const serialized = JSON.stringify(result);

      if (serialized !== lastDataRef.current) {
        lastDataRef.current = serialized;
        startTimeRef.current = Date.now();
        setLastChanged(new Date());
      }

      setData(result);
      setError(null);

      if (isCompleteRef.current?.(result)) {
        completedRef.current = true;
      }
    } catch (e) {
      const error = e instanceof Error ? e : new Error(String(e));
      setError(error);
      if (shouldRetryRef.current?.(error) === false) completedRef.current = true;
    } finally {
      setIsLoading(false);
    }
  }, [fetcher]);

  useEffect(() => {
    if (!enabled) return;

    completedRef.current = false;
    setData(null);
    setError(null);
    startTimeRef.current = Date.now();
    lastDataRef.current = "";
    poll();

    const timer = setInterval(() => {
      if (completedRef.current) {
        clearInterval(timer);
        return;
      }

      const elapsed = Date.now() - startTimeRef.current;
      const currentInterval =
        elapsed > idleAfterMs
          ? idleInterval
          : elapsed > slowAfterMs
            ? slowInterval
            : initialInterval;

      const timeSinceStart = elapsed % currentInterval;
      if (timeSinceStart < initialInterval) {
        poll();
      }
    }, initialInterval);

    return () => clearInterval(timer);
  }, [enabled, poll, initialInterval, slowInterval, slowAfterMs, idleInterval, idleAfterMs]);

  return { data, isLoading, error, lastChanged };
}
