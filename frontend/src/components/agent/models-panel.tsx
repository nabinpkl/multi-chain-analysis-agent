"use client";

import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";

import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  useLlmOverride,
  type ProviderId,
  type Role,
} from "@/stores/use-llm-override";

/**
 * Builder-view section for per-role LLM provider override (OpenRouter
 * vs local LM Studio). Three rows: Primary, Policy, Judge. Each row
 * lets a developer pin the model that role uses for the next chat
 * turn; choices persist to localStorage via the `useLlmOverride`
 * store and are stamped onto every `/agent/ask` request via
 * `use-agent-stream.ts`. Production frontend never renders this
 * panel; production traffic carries an empty `LlmOverride` and the
 * backend defaults to env-driven OpenRouter.
 *
 * The local-model dropdown is populated by hitting
 * `${NEXT_PUBLIC_AGENT_URL}/agent/local/models`, which the
 * agent-service proxies to LM Studio's `/v1/models` server-side. The
 * proxy returns one canonical shape (`{reachable, baseUrl, models}`)
 * regardless of failure, so this component renders one error state.
 */

interface LocalModel {
  id: string;
  object?: string;
}

interface ModelsResponse {
  reachable: boolean;
  baseUrl: string;
  models: LocalModel[];
}

const ROLES: { id: Role; label: string; hint: string }[] = [
  {
    id: "primary",
    label: "primary",
    hint: "the agent that picks tools and writes the narrative.",
  },
  {
    id: "policy",
    label: "policy",
    hint: "the constitution gate + repeat detector. Both share the same policy-tier model.",
  },
  {
    id: "judge",
    label: "judge",
    hint: "the eval-substrate judge. Not used in the chat flow today; kept on the wire for symmetry.",
  },
];

function agentUrl(): string {
  return process.env.NEXT_PUBLIC_AGENT_URL || "http://localhost:8003";
}

export function ModelsPanel() {
  const primary = useLlmOverride((s) => s.primary);
  const policy = useLlmOverride((s) => s.policy);
  const judge = useLlmOverride((s) => s.judge);
  const setOverride = useLlmOverride((s) => s.setOverride);

  const overrides: Record<Role, typeof primary> = { primary, policy, judge };

  const [open, setOpen] = useState(false);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [models, setModels] = useState<LocalModel[]>([]);
  const [refreshing, setRefreshing] = useState(false);

  const fetchModels = useCallback(async () => {
    setRefreshing(true);
    try {
      const res = await fetch(`${agentUrl()}/agent/local/models`, {
        cache: "no-store",
      });
      if (!res.ok) {
        setReachable(false);
        setModels([]);
        return;
      }
      const body: ModelsResponse = await res.json();
      setReachable(body.reachable);
      setModels(body.models ?? []);
    } catch {
      setReachable(false);
      setModels([]);
    } finally {
      setRefreshing(false);
    }
  }, []);

  // Lazy-fetch the first time the section opens; refresh button after
  // that. Avoids burning a request on a closed panel.
  useEffect(() => {
    if (open && reachable === null) {
      void fetchModels();
    }
  }, [open, reachable, fetchModels]);

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <div className="border-b border-mca-border bg-mca-surface-raised">
        <CollapsibleTrigger
          className="w-full flex items-center justify-between px-4 py-2 hover:bg-mca-surface focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent"
          aria-label="toggle models section"
        >
          <span className="flex items-center gap-2 text-[0.55rem] uppercase tracking-[1.5px] text-mca-muted">
            {open ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            models
            <ReachabilityDot reachable={reachable} />
          </span>
          {open ? (
            <button
              type="button"
              className="text-mca-muted hover:text-mca-text focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent rounded p-1"
              aria-label="refresh local models list"
              onClick={(e) => {
                e.stopPropagation();
                void fetchModels();
              }}
              disabled={refreshing}
            >
              <RefreshCw
                className={`h-3 w-3 ${refreshing ? "animate-spin" : ""}`}
              />
            </button>
          ) : null}
        </CollapsibleTrigger>
        <CollapsibleContent>
          <div className="px-4 py-3 space-y-3">
            {ROLES.map(({ id, label, hint }) => (
              <RoleRow
                key={id}
                role={id}
                label={label}
                hint={hint}
                override={overrides[id]}
                models={models}
                reachable={reachable}
                onChange={(o) => setOverride(id, o)}
              />
            ))}
            <p className="text-[0.6rem] text-mca-muted leading-relaxed pt-1">
              Local model must support OpenAI-style tool calls AND
              JSON-schema structured output. Recommended:
              Qwen2.5-7B-Instruct, Llama-3.1-8B-Instruct, or recent
              Mistral. Smaller / older models will silently produce
              empty primary turns.
            </p>
          </div>
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}

function ReachabilityDot({ reachable }: { reachable: boolean | null }) {
  if (reachable === null) {
    return (
      <span
        className="inline-block w-2 h-2 rounded-full bg-mca-muted/40"
        aria-label="local model server reachability not yet checked"
      />
    );
  }
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${
        reachable ? "bg-emerald-500" : "bg-rose-500"
      }`}
      aria-label={
        reachable
          ? "local model server reachable"
          : "local model server not reachable"
      }
    />
  );
}

function RoleRow({
  role,
  label,
  hint,
  override,
  models,
  reachable,
  onChange,
}: {
  role: Role;
  label: string;
  hint: string;
  override: { provider: ProviderId; modelId: string };
  models: LocalModel[];
  reachable: boolean | null;
  onChange: (o: { provider: ProviderId; modelId: string }) => void;
}) {
  // Treat empty provider as "openrouter" for the segment UI: that's
  // what the empty state means (use the production default), and the
  // default is OpenRouter. Local provider always renders explicitly.
  const effectiveProvider: "openrouter" | "local" =
    override.provider === "local" ? "local" : "openrouter";

  // Stale model id: developer chose a model name, then unloaded it
  // in LM Studio. We don't auto-clear it (the user might reload it
  // shortly), but we flag it red so they know to refresh + repick.
  const modelKnown =
    override.modelId === "" ||
    models.some((m) => m.id === override.modelId);

  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[0.7rem] text-mca-text">{label}</span>
        <Segment
          value={effectiveProvider}
          onChange={(v) => {
            if (v === "openrouter") {
              onChange({ provider: "", modelId: "" });
            } else {
              onChange({ provider: "local", modelId: override.modelId });
            }
          }}
        />
      </div>
      <p className="text-[0.6rem] text-mca-muted leading-snug">{hint}</p>
      {effectiveProvider === "local" ? (
        <select
          className={`w-full text-[0.7rem] bg-mca-bg border rounded px-2 py-1 focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent ${
            modelKnown ? "border-mca-border" : "border-rose-500"
          }`}
          value={override.modelId}
          onChange={(e) =>
            onChange({ provider: "local", modelId: e.target.value })
          }
          disabled={reachable === false && models.length === 0}
          aria-label={`local model id for ${role}`}
        >
          <option value="">
            {reachable === false
              ? "lm studio not reachable"
              : "select a model"}
          </option>
          {!modelKnown && override.modelId ? (
            <option value={override.modelId}>
              {override.modelId} (not loaded)
            </option>
          ) : null}
          {models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.id}
            </option>
          ))}
        </select>
      ) : (
        <p className="text-[0.6rem] text-mca-muted italic">
          uses env-default openrouter model id
        </p>
      )}
    </div>
  );
}

function Segment({
  value,
  onChange,
}: {
  value: "openrouter" | "local";
  onChange: (v: "openrouter" | "local") => void;
}) {
  return (
    <div className="inline-flex rounded border border-mca-border bg-mca-bg overflow-hidden">
      <SegmentButton
        active={value === "openrouter"}
        onClick={() => onChange("openrouter")}
      >
        openrouter
      </SegmentButton>
      <SegmentButton
        active={value === "local"}
        onClick={() => onChange("local")}
      >
        local
      </SegmentButton>
    </div>
  );
}

function SegmentButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`text-[0.6rem] uppercase tracking-[1.5px] px-2 py-1 transition-colors ${
        active
          ? "bg-mca-accent text-mca-bg"
          : "text-mca-muted hover:text-mca-text"
      }`}
    >
      {children}
    </button>
  );
}
