"use client";

import { useEffect, useState } from "react";
import { History, Trash2 } from "lucide-react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useThreadHistory } from "@/stores/use-thread-history";
import { useRuntimeSelector } from "@/stores/use-runtime-selector";
import { AgentRuntime } from "@/lib/wire/multichain/wire/agent/v1/session_pb";
import type { AgentStreamState } from "@/hooks/use-agent-stream";

/**
 * Chunk 4 history dropdown. Sits in the agent-sheet header chrome,
 * triggered by a `History` lucide icon. Lists past threads
 * (newest first) so the user can reopen a conversation across
 * page refreshes / browser restarts.
 *
 * Behavior cribbed from second-brain's `agent-panel.tsx` dropdown:
 * one row per thread, runtime chip + truncated title +
 * last-question preview. Click a row -> `loadThread(threadId)`
 * hydrates the chat scroll with the full transcript via
 * `GET /agent/thread/{id}`. The runtime selector is force-synced
 * to the loaded thread's runtime so the chip + chrome match the
 * server's persisted lock.
 *
 * Archive is a soft delete (Trash2 icon on each row); the row
 * disappears from the default list but the on-disk state stays
 * for unarchive-and-resume later. Toggle "show archived" surfaces
 * archived rows for that workflow.
 */
export function HistoryMenu({
  agentStream,
}: {
  agentStream: AgentStreamState;
}) {
  const [open, setOpen] = useState(false);
  const {
    threads,
    loading,
    error,
    includeArchived,
    setIncludeArchived,
    refresh,
    archive,
  } = useThreadHistory();
  const setRuntime = useRuntimeSelector((s) => s.setRuntime);
  const activeThreadId = agentStream.threadId;

  // Fetch on open. Closing doesn't tear down the list; subsequent
  // opens trigger a fresh fetch so the list reflects server-side
  // changes (e.g. a new turn just landed on a thread).
  useEffect(() => {
    if (open) {
      void refresh();
    }
  }, [open, refresh, includeArchived]);

  async function onSelect(threadId: string, runtime: AgentRuntime) {
    setOpen(false);
    // Sync the runtime selector to the loaded thread's runtime
    // BEFORE hydrating turns so the chip in the chrome doesn't
    // briefly flash the wrong value.
    setRuntime(runtime);
    await agentStream.loadThread(threadId);
  }

  async function onArchive(
    e: React.MouseEvent,
    threadId: string,
  ) {
    e.stopPropagation();
    e.preventDefault();
    await archive(threadId);
  }

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger
        className="text-mca-muted hover:text-mca-text transition-colors p-1 rounded border border-mca-border"
        title="thread history"
        aria-label="open thread history"
      >
        <History className="h-3.5 w-3.5" />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        sideOffset={6}
        className="w-[320px] max-h-[60vh] overflow-y-auto p-0"
      >
        <div className="px-3 py-2 border-b border-mca-border flex items-center justify-between gap-2 sticky top-0 bg-mca-surface-raised z-10">
          <span className="text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
            history
          </span>
          <label className="flex items-center gap-1.5 cursor-pointer text-[0.55rem] text-mca-muted hover:text-mca-text">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(e) => setIncludeArchived(e.target.checked)}
              className="accent-mca-accent h-3 w-3"
            />
            show archived
          </label>
        </div>
        {loading ? (
          <div className="px-3 py-4 text-[0.65rem] text-mca-muted">
            loading
          </div>
        ) : error ? (
          <div className="px-3 py-4 text-[0.65rem] text-mca-accent-mid">
            {error}
          </div>
        ) : threads.length === 0 ? (
          <div className="px-3 py-4 text-[0.65rem] text-mca-muted">
            no threads yet
          </div>
        ) : (
          <ul className="divide-y divide-mca-border">
            {threads.map((t) => (
              <li key={t.threadId}>
                <button
                  type="button"
                  onClick={() => onSelect(t.threadId, t.runtime)}
                  className={`w-full text-left px-3 py-2 hover:bg-mca-surface transition-colors flex items-start gap-2 ${
                    activeThreadId === t.threadId
                      ? "bg-mca-surface"
                      : ""
                  }`}
                >
                  <RuntimeChip runtime={t.runtime} />
                  <span className="flex-1 min-w-0">
                    <span className="block text-[0.7rem] text-mca-text truncate">
                      {t.title || "(untitled)"}
                    </span>
                    <span className="block text-[0.55rem] text-mca-muted truncate">
                      {t.lastUserQuestion || "no messages yet"}
                    </span>
                    <span className="block text-[0.5rem] text-mca-dim tabular-nums mt-0.5">
                      turn {t.turnCount} · {relativeTime(Number(t.startedAtMs))}
                      {t.archived ? " · archived" : ""}
                    </span>
                  </span>
                  <button
                    type="button"
                    onClick={(e) => onArchive(e, t.threadId)}
                    className="text-mca-dim hover:text-mca-accent-mid p-1"
                    title={t.archived ? "already archived" : "archive thread"}
                    disabled={t.archived}
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </button>
              </li>
            ))}
          </ul>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Runtime-keyed visual chip. Same color tokens the chrome cue uses
 * (amber for codex, blue for pydantic-ai) so the chip in the
 * dropdown row matches the chrome's active-runtime pill the user
 * sees AFTER clicking a row.
 */
function RuntimeChip({ runtime }: { runtime: AgentRuntime }) {
  const isCodex = runtime === AgentRuntime.CODEX;
  return (
    <span
      className={`text-[0.45rem] uppercase tracking-[1.5px] font-mono shrink-0 rounded px-1 py-0.5 border ${
        isCodex
          ? "border-amber-500/40 text-amber-600 bg-amber-500/5"
          : "border-sky-500/40 text-sky-600 bg-sky-500/5"
      }`}
    >
      {isCodex ? "codex" : "pydantic"}
    </span>
  );
}

/**
 * Lightweight relative-time formatter. Intl.RelativeTimeFormat is
 * built into the browser; no library needed for our two
 * granularities ("3m ago" / "2d ago").
 */
function relativeTime(startedAtMs: number): string {
  if (!startedAtMs) return "";
  const now = Date.now();
  const diff = now - startedAtMs;
  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (days >= 1) return rtf.format(-days, "day");
  if (hours >= 1) return rtf.format(-hours, "hour");
  if (minutes >= 1) return rtf.format(-minutes, "minute");
  return "just now";
}
