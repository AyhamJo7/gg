/** Bearer-token delivery for the local-only auth boundary (see backend
 * orchestrator/api/auth.py). There is deliberately no network route to fetch
 * this token — any local process could otherwise just curl it. Instead:
 *  - Tauri shell: the Rust side already spawned `gg-backend` and knows where
 *    its token file landed; `get_auth_token` is a Tauri IPC command (not a
 *    network route — unreachable from arbitrary web content) that reads it.
 *  - Browser dev/build: Vite embeds the token at dev-server/build time as
 *    `VITE_AUTH_TOKEN` (see vite.config.ts), since a page has no filesystem
 *    access to read it itself.
 */

export const isTauri: boolean =
  typeof window !== "undefined" && ("__TAURI_INTERNALS__" in window || window.location.origin.startsWith("tauri://"));

let cached: Promise<string | null> | null = null;

export function getAuthToken(): Promise<string | null> {
  if (cached) return cached;
  cached = (async () => {
    if (isTauri && window.__TAURI_INTERNALS__) {
      try {
        const token = await window.__TAURI_INTERNALS__.invoke("get_auth_token");
        return typeof token === "string" && token.length > 0 ? token : null;
      } catch {
        return null;
      }
    }
    const embedded = import.meta.env.VITE_AUTH_TOKEN as string | undefined;
    return embedded && embedded.length > 0 ? embedded : null;
  })();
  return cached;
}

declare global {
  interface Window {
    __TAURI_INTERNALS__?: {
      invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
    };
  }
}
