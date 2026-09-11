import { useCallback, useEffect, useRef, useState } from "react";

/** Poll an async function. intervalMs=null pauses. */
export function usePolling<T>(
  fn: () => Promise<T>,
  intervalMs: number | null,
  deps: unknown[] = [],
): { data: T | null; error: string | null; refresh: () => void } {
  const key = JSON.stringify(deps);
  const [result, setResult] = useState<{ key: string; value: T } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const generation = useRef(0);
  const request = useRef(0);

  const refresh = useCallback(() => {
    const epoch = generation.current;
    const sequence = ++request.current;
    return Promise.resolve().then(() => fnRef.current())
      .then((v) => {
        if (epoch !== generation.current || sequence !== request.current) return;
        setResult({ key, value: v });
        setError(null);
      })
      .catch((e: unknown) => {
        if (epoch === generation.current && sequence === request.current)
          setError(e instanceof Error ? e.message : String(e));
      });
  }, [key]);

  useEffect(() => {
    const epoch = ++generation.current;
    setError(null);
    let id: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;
    const tick = () => {
      void refresh().finally(() => {
        if (!stopped && intervalMs !== null) id = setTimeout(tick, intervalMs);
      });
    };
    tick();
    return () => { stopped = true; generation.current = epoch + 1; if (id !== null) clearTimeout(id); };
  }, [intervalMs, refresh]);

  return { data: result?.key === key ? result.value : null, error, refresh };
}

export function useElapsed(startIso: string | null, active: boolean): string {
  const [, force] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [active]);
  if (!startIso) return "—";
  const ms = Date.now() - new Date(startIso).getTime();
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0
    ? `${h}h ${String(m).padStart(2, "0")}m`
    : `${m}m ${String(sec).padStart(2, "0")}s`;
}
