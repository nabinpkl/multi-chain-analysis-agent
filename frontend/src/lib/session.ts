/**
 * Browser-stable identity helpers backed by `localStorage`.
 *
 * Two distinct ids live here:
 *
 *   - `sessionId`: minted on first load, persists for the browser's
 *     lifetime. Namespaces all zustand-persisted preferences (switches,
 *     models config, etc.) so a future "this browser is signed in as
 *     <user>" migration can derive the namespace from the authenticated
 *     user id without losing in-flight state. Does NOT cross the wire
 *     today; defer until auth lands.
 *
 *   - `threadId`: the current conversation handle. Sent in every
 *     `POST /agent/turn` body; server returns 404 when the id is stale,
 *     which the hook recovers from by clearing this value and retrying.
 *     Cleared by the "new chat" button.
 *
 * SSR guard: `localStorage` is only available in the browser. Helpers
 * return null when called server-side; the matching pattern is already
 * used in `frontend/src/lib/analytics.ts`.
 */

const SESSION_ID_KEY = "mca:sessionId";
const THREAD_ID_KEY = "mca:threadId";

function browser(): boolean {
  return typeof window !== "undefined";
}

/**
 * Returns the browser-stable session id, minting one on first call.
 * SSR-safe: returns "ssr" when called server-side so namespace keys
 * are deterministic at render time. The real id replaces it on first
 * client-side render (zustand's persist middleware hydrates after
 * mount).
 */
export function getSessionId(): string {
  if (!browser()) return "ssr";
  let id = window.localStorage.getItem(SESSION_ID_KEY);
  if (!id) {
    id = crypto.randomUUID();
    window.localStorage.setItem(SESSION_ID_KEY, id);
  }
  return id;
}

/** Read the current thread id, or null if none stored. */
export function getThreadId(): string | null {
  if (!browser()) return null;
  return window.localStorage.getItem(THREAD_ID_KEY);
}

/** Persist a fresh thread id. */
export function setThreadId(id: string): void {
  if (!browser()) return;
  window.localStorage.setItem(THREAD_ID_KEY, id);
}

/** Drop the current thread id ("new chat" or 404 recovery). */
export function clearThreadId(): void {
  if (!browser()) return;
  window.localStorage.removeItem(THREAD_ID_KEY);
}

/** Build a stable zustand-persist `name` namespaced under the session. */
export function namespacedStoreName(storeName: string): string {
  return `mca:${getSessionId()}:${storeName}`;
}
