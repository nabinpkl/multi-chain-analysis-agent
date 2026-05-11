"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { useRuntimeSelector } from "@/stores/use-runtime-selector";
import { AgentRuntime } from "@/lib/wire/multichain/wire/agent/v1/session_pb";

/**
 * Builder-view section for picking which backend agent runtime
 * handles the next chat. Two radios: `pydantic-ai` (the in-process
 * loop) and `codex` (the JSON-RPC bridge to the codex-cli
 * subprocess, exposed by `agent_service/codex_driver.py`).
 *
 * Runtime is per-thread locked at thread creation: the backend
 * writes `<thread_root>/threads/<thread_id>/runtime.json` on
 * mint and returns 400 on any later turn whose `runtime` field
 * disagrees. We mirror that lock in the UI by disabling the
 * radios when a thread is open (`threadId !== null`); clicking
 * "new" in the agent-sheet header clears the thread and re-enables
 * the toggle.
 *
 * Production frontend never renders this panel  the agent-sheet
 * gates the whole builder strip behind `builderViewOn`, so
 * production traffic carries the default runtime (pydantic-ai)
 * without a code path for switching.
 */
export function RuntimePanel({
  threadId,
}: {
  /** Current thread id from `useAgentStream`. When non-null the
   *  runtime is locked and radios disable. */
  threadId: string | null;
}) {
  const [open, setOpen] = useState(true);
  const runtime = useRuntimeSelector((s) => s.runtime);
  const setRuntime = useRuntimeSelector((s) => s.setRuntime);
  const locked = threadId !== null;

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <div className="border-b border-mca-border bg-mca-surface-raised">
        <CollapsibleTrigger
          className="w-full flex items-center justify-between px-4 py-2 hover:bg-mca-surface focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent"
          aria-label="toggle runtime section"
        >
          <span className="flex items-center gap-2 text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
            {open ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            runtime
            {locked ? (
              <span
                className="text-[0.5rem] tabular-nums normal-case text-mca-dim"
                title="runtime is locked once a thread exists; click 'new' to switch"
              >
                locked
              </span>
            ) : null}
          </span>
          <span className="text-[0.55rem] tabular-nums text-mca-text normal-case">
            {runtime === AgentRuntime.CODEX ? "codex" : "pydantic-ai"}
          </span>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="px-4 py-3 space-y-2">
            <RuntimeRadio
              id="pydantic-ai"
              label="pydantic-ai"
              hint="in-process pydantic-ai loop. Default. Carries constitution + structural + placeholder gates over emitted claims."
              selected={runtime === AgentRuntime.PYDANTIC_AI}
              disabled={locked}
              onSelect={() => setRuntime(AgentRuntime.PYDANTIC_AI)}
            />
            <RuntimeRadio
              id="codex"
              label="codex"
              hint="codex-cli subprocess via JSON-RPC + HTTP MCP. Primary model is gpt-5-codex; tools route through the Rust /mcp surface."
              selected={runtime === AgentRuntime.CODEX}
              disabled={locked}
              onSelect={() => setRuntime(AgentRuntime.CODEX)}
            />
            <p className="text-[0.6rem] text-mca-muted leading-relaxed pt-1">
              {locked
                ? "runtime is locked while a thread is open. click 'new' in the header above to start a fresh thread and pick a different runtime."
                : "pick which agent runtime handles the next turn. choice is per-developer (persists to localStorage) and stamps on every outgoing /agent/turn request."}
            </p>
          </div>
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}

function RuntimeRadio({
  id,
  label,
  hint,
  selected,
  disabled,
  onSelect,
}: {
  id: string;
  label: string;
  hint: string;
  selected: boolean;
  disabled: boolean;
  onSelect: () => void;
}) {
  return (
    <label
      htmlFor={`runtime-${id}`}
      className={`flex items-start gap-2 cursor-pointer rounded px-2 py-1.5 transition-colors ${
        disabled
          ? "opacity-50 cursor-not-allowed"
          : selected
            ? "bg-mca-surface"
            : "hover:bg-mca-surface/60"
      }`}
    >
      <input
        type="radio"
        id={`runtime-${id}`}
        name="runtime-selector"
        checked={selected}
        disabled={disabled}
        onChange={onSelect}
        className="mt-0.5 accent-mca-accent"
      />
      <span className="flex-1 min-w-0">
        <span className="block text-[0.7rem] text-mca-text">{label}</span>
        <span className="block text-[0.6rem] text-mca-muted leading-snug">
          {hint}
        </span>
      </span>
    </label>
  );
}
