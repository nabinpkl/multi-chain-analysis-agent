"use client";

import { create as createStore } from "zustand";
import { fromJsonString } from "@bufbuild/protobuf";

import {
  ThreadListSchema,
  type ThreadSummary,
} from "@/lib/wire/multichain/wire/agent/v1/history_pb";

/**
 * Chunk 4 history store. Owns the list of past threads the
 * `HistoryMenu` dropdown renders. Backed by `GET /agent/threads`
 * on demand (no in-memory cache that needs invalidation; the
 * dropdown explicitly calls `refresh()` when it opens, and
 * `archive()` / "new chat" invalidate by triggering another
 * fetch on next open).
 *
 * We deliberately don't use `@tanstack/react-query` here even
 * though it's in deps  no `QueryClientProvider` is mounted in
 * the app, and history list size + cadence don't justify
 * standing one up. Plain useState/useEffect-equivalent state
 * via zustand keeps the dependency footprint flat.
 */

const DEFAULT_AGENT_URL = "http://localhost:8003";

function agentUrl(): string {
  return process.env.NEXT_PUBLIC_AGENT_URL || DEFAULT_AGENT_URL;
}

export interface ThreadHistoryStore {
  /** Currently-loaded list of thread summaries (newest first).
   *  Empty until `refresh()` runs. */
  threads: ThreadSummary[];
  /** True while a fetch is in flight. UI shows a small spinner. */
  loading: boolean;
  /** Surface for transient fetch errors. Cleared on next refresh. */
  error: string | null;
  /** When true, archived threads are included in `threads`. The
   *  dropdown toggles this with a small "show archived" filter. */
  includeArchived: boolean;
  setIncludeArchived: (v: boolean) => void;
  /** Re-fetch the list. Awaitable so callers can chain
   *  archive -> refresh without flashing stale UI. */
  refresh: () => Promise<void>;
  /** Soft-archive a thread server-side, then refresh the list.
   *  Idempotent on the server; returns true when the archive
   *  call succeeded. */
  archive: (threadId: string) => Promise<boolean>;
}

export const useThreadHistory = createStore<ThreadHistoryStore>()(
  (set, get) => ({
    threads: [],
    loading: false,
    error: null,
    includeArchived: false,
    setIncludeArchived: (v) => {
      set({ includeArchived: v });
      // Caller can `.refresh()` after; we don't auto-fire here
      // because the dropdown opens with a fresh fetch anyway.
    },
    refresh: async () => {
      set({ loading: true, error: null });
      try {
        const url = new URL(`${agentUrl()}/agent/threads`);
        if (get().includeArchived) {
          url.searchParams.set("include_archived", "true");
        }
        const resp = await fetch(url.toString(), { method: "GET" });
        if (!resp.ok) {
          set({
            loading: false,
            error: `failed to load history: ${resp.status}`,
            threads: [],
          });
          return;
        }
        const body = await resp.text();
        const parsed = fromJsonString(ThreadListSchema, body);
        set({
          loading: false,
          threads: parsed.threads,
          error: null,
        });
      } catch (e) {
        set({
          loading: false,
          error: `failed to load history: ${
            e instanceof Error ? e.message : String(e)
          }`,
          threads: [],
        });
      }
    },
    archive: async (threadId) => {
      try {
        const resp = await fetch(
          `${agentUrl()}/agent/thread/${threadId}/archive`,
          { method: "POST" },
        );
        if (!resp.ok && resp.status !== 404) {
          set({ error: `archive failed: ${resp.status}` });
          return false;
        }
        await get().refresh();
        return true;
      } catch (e) {
        set({
          error: `archive failed: ${
            e instanceof Error ? e.message : String(e)
          }`,
        });
        return false;
      }
    },
  }),
);
