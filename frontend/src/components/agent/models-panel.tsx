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
import { useCodexOverride } from "@/stores/use-codex-override";
import { useRuntimeSelector } from "@/stores/use-runtime-selector";
import { AgentRuntime } from "@/lib/wire/multichain/wire/agent/v1/session_pb";
import { useRoleTimings } from "@/stores/use-role-timings";

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
 * Two model-list lookups, both proxied through agent-service to
 * sidestep browser CORS:
 *   - `/agent/local/models` → LM Studio's `/v1/models`. Lists
 *     whatever the dev has loaded in LM Studio.
 *   - `/agent/openrouter/models` → OpenRouter's public `/v1/models`,
 *     server-side filtered to ids ending in `:free`. Lets the dev
 *     swap the production-default OpenRouter model (currently
 *     timeout-prone on the primary role) for a faster `:free`
 *     sibling without editing `.env`. Picking "(env default)"
 *     restores `AGENT_*_MODEL` behavior.
 *
 * Both proxies return one canonical shape regardless of failure mode
 * so this component renders one error state per provider.
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

interface OpenRouterModel {
  id: string;
  name?: string;
  contextLength?: number | null;
}

interface OpenRouterModelsResponse {
  reachable: boolean;
  models: OpenRouterModel[];
}

interface GeminiModel {
  id: string;
  name?: string;
}

interface GeminiModelsResponse {
  reachable: boolean;
  models: GeminiModel[];
}

interface CodexModel {
  id: string;
  name?: string;
}

interface CodexModelsResponse {
  reachable: boolean;
  models: CodexModel[];
  reasoningEfforts: string[];
}

// Role defaults extends the pydantic-ai roles with codex-runtime
// fallbacks so the panel can label the env-default option with the
// actual configured value for both runtime branches.
interface RoleDefaults {
  primary: string;
  policy: string;
  judge: string;
  codexPrimary: string;
  codexReasoningEffort: string;
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
  const codexModelId = useCodexOverride((s) => s.modelId);
  const codexReasoningEffort = useCodexOverride((s) => s.reasoningEffort);
  const setCodexModel = useCodexOverride((s) => s.setModelId);
  const setCodexEffort = useCodexOverride((s) => s.setReasoningEffort);
  const runtime = useRuntimeSelector((s) => s.runtime);
  // `runtime === CODEX` toggles which "primary" surface the panel
  // shows: the codex model+effort row replaces the pydantic-ai
  // openrouter/gemini/local row. Policy + judge always render
  // through pydantic-ai (the constitution agent runs server-side
  // via pydantic-ai regardless of primary runtime, and the judge
  // is eval-only).
  const isCodex = runtime === AgentRuntime.CODEX;

  const overrides: Record<Role, typeof primary> = { primary, policy, judge };
  const latestTimings = useRoleTimings((s) => s.latest);

  const [open, setOpen] = useState(false);
  const [reachable, setReachable] = useState<boolean | null>(null);
  const [models, setModels] = useState<LocalModel[]>([]);
  const [orReachable, setOrReachable] = useState<boolean | null>(null);
  const [orModels, setOrModels] = useState<OpenRouterModel[]>([]);
  const [gmReachable, setGmReachable] = useState<boolean | null>(null);
  const [gmModels, setGmModels] = useState<GeminiModel[]>([]);
  const [cxReachable, setCxReachable] = useState<boolean | null>(null);
  const [cxModels, setCxModels] = useState<CodexModel[]>([]);
  const [cxEfforts, setCxEfforts] = useState<string[]>([]);
  const [defaults, setDefaults] = useState<RoleDefaults>({
    primary: "",
    policy: "",
    judge: "",
    codexPrimary: "",
    codexReasoningEffort: "",
  });
  const [refreshing, setRefreshing] = useState(false);

  // Both lookups run in parallel and share the refresh button +
  // spinner. They're independent failure surfaces (LM Studio can be
  // down while OpenRouter is up and vice versa); each surface
  // rendered with its own reachability dot in the row UIs below.
  const fetchModels = useCallback(async () => {
    setRefreshing(true);
    const localPromise = (async () => {
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
      }
    })();
    const orPromise = (async () => {
      try {
        const res = await fetch(`${agentUrl()}/agent/openrouter/models`, {
          cache: "no-store",
        });
        if (!res.ok) {
          setOrReachable(false);
          setOrModels([]);
          return;
        }
        const body: OpenRouterModelsResponse = await res.json();
        setOrReachable(body.reachable);
        setOrModels(body.models ?? []);
      } catch {
        setOrReachable(false);
        setOrModels([]);
      }
    })();
    const gmPromise = (async () => {
      try {
        const res = await fetch(`${agentUrl()}/agent/gemini/models`, {
          cache: "no-store",
        });
        if (!res.ok) {
          setGmReachable(false);
          setGmModels([]);
          return;
        }
        const body: GeminiModelsResponse = await res.json();
        setGmReachable(body.reachable);
        setGmModels(body.models ?? []);
      } catch {
        setGmReachable(false);
        setGmModels([]);
      }
    })();
    const defaultsPromise = (async () => {
      try {
        const res = await fetch(`${agentUrl()}/agent/config/role-defaults`, {
          cache: "no-store",
        });
        if (!res.ok) return;
        const body = (await res.json()) as Partial<RoleDefaults>;
        setDefaults({
          primary: body.primary ?? "",
          policy: body.policy ?? "",
          judge: body.judge ?? "",
          codexPrimary: body.codexPrimary ?? "",
          codexReasoningEffort: body.codexReasoningEffort ?? "",
        });
      } catch {
        // Defaults stay as their zero-value placeholders; the
        // dropdown falls back to the generic "(env default)" label
        // when the per-role string is empty.
      }
    })();
    const cxPromise = (async () => {
      // The codex catalog is in-process on the agent service (codex-cli
      // doesn't ship a list-models endpoint), so this fetch is fast and
      // can't fail in the "unreachable" sense unless agent-service
      // itself is down. We still treat a non-2xx the same way the
      // other panels do.
      try {
        const res = await fetch(`${agentUrl()}/agent/codex/models`, {
          cache: "no-store",
        });
        if (!res.ok) {
          setCxReachable(false);
          setCxModels([]);
          setCxEfforts([]);
          return;
        }
        const body: CodexModelsResponse = await res.json();
        setCxReachable(body.reachable);
        setCxModels(body.models ?? []);
        setCxEfforts(body.reasoningEfforts ?? []);
      } catch {
        setCxReachable(false);
        setCxModels([]);
        setCxEfforts([]);
      }
    })();
    try {
      await Promise.all([
        localPromise,
        orPromise,
        gmPromise,
        cxPromise,
        defaultsPromise,
      ]);
    } finally {
      setRefreshing(false);
    }
  }, []);

  // Lazy-fetch the first time the section opens; refresh button after
  // that. Avoids burning a request on a closed panel. The trigger
  // condition is "any lookup is uninitialized" so a fresh page load
  // primes all three even if one is down on first try.
  useEffect(() => {
    if (
      open &&
      (reachable === null ||
        orReachable === null ||
        gmReachable === null ||
        cxReachable === null)
    ) {
      void fetchModels();
    }
  }, [open, reachable, orReachable, gmReachable, cxReachable, fetchModels]);

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
              aria-label="refresh local + openrouter models lists"
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
            {ROLES.map(({ id, label, hint }) => {
              // The "primary" role row is runtime-conditional: codex
              // turns are driven by codex-cli (not OpenRouter / LM
              // Studio / Gemini), so the panel swaps in a codex-shaped
              // row with model + reasoning-effort selectors. Policy +
              // judge always render the pydantic-ai shape because the
              // constitution agent + judge run via pydantic-ai
              // regardless of primary runtime.
              if (id === "primary" && isCodex) {
                return (
                  <CodexPrimaryRow
                    key={id}
                    modelId={codexModelId}
                    reasoningEffort={codexReasoningEffort}
                    models={cxModels}
                    reachable={cxReachable}
                    reasoningEfforts={cxEfforts}
                    envDefaultModel={defaults.codexPrimary}
                    envDefaultEffort={defaults.codexReasoningEffort}
                    lastTurnMs={
                      latestTimings === null
                        ? null
                        : latestTimings.primaryMs
                    }
                    onModelChange={setCodexModel}
                    onEffortChange={setCodexEffort}
                  />
                );
              }
              return (
                <RoleRow
                  key={id}
                  role={id}
                  label={label}
                  hint={hint}
                  override={overrides[id]}
                  models={models}
                  reachable={reachable}
                  orModels={orModels}
                  orReachable={orReachable}
                  gmModels={gmModels}
                  gmReachable={gmReachable}
                  envDefaultModelId={defaults[id]}
                  lastTurnMs={
                    latestTimings === null
                      ? null
                      : id === "primary"
                        ? latestTimings.primaryMs
                        : id === "policy"
                          ? latestTimings.policyMs
                          : latestTimings.judgeMs
                  }
                  onChange={(o) => setOverride(id, o)}
                />
              );
            })}
            <p className="text-[0.6rem] text-mca-muted leading-relaxed pt-1">
              {isCodex
                ? "codex primary uses codex-cli's bundled model catalog and reasoning-effort tiers. policy + judge still route via pydantic-ai (the constitution agent and eval judge are runtime-agnostic). empty model / effort = fall through to CODEX_PRIMARY_MODEL / CODEX_REASONING_EFFORT env, then codex-cli's own defaults."
                : "gemini lists every model exposed by Google's OpenAI-compat endpoint (Gemma open-weights + Gemini proprietary). openrouter lists `:free` ids only. local hits LM Studio at LOCAL_LLM_BASE_URL. Local + Gemma open-weights must support OpenAI-style tool calls AND JSON-schema structured output for the agent loop to function; models that ignore tool_choice will produce empty primary turns."}
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
  orModels,
  orReachable,
  gmModels,
  gmReachable,
  envDefaultModelId,
  lastTurnMs,
  onChange,
}: {
  role: Role;
  label: string;
  hint: string;
  override: { provider: ProviderId; modelId: string };
  models: LocalModel[];
  reachable: boolean | null;
  orModels: OpenRouterModel[];
  orReachable: boolean | null;
  gmModels: GeminiModel[];
  gmReachable: boolean | null;
  envDefaultModelId: string;
  /** Last completed turn's wall-time for this role, ms.
   *  `null` before any turn has finished on this session;
   *  `0` when the role didn't fire on the last turn (e.g. judge
   *  always 0 today). */
  lastTurnMs: number | null;
  onChange: (o: { provider: ProviderId; modelId: string }) => void;
}) {
  // Three explicit provider segments. Empty `override.provider` means
  // "use the production default", and which default is in effect is
  // controlled by the backend's `AGENT_DEFAULT_PROVIDER` env (gemini
  // today). The segment UI maps empty to "gemini" when that's the
  // default and "openrouter" otherwise so the user sees the segment
  // their next request will actually route through, not a stale
  // OpenRouter pin from when this UI was built.
  //
  // We can't read `AGENT_DEFAULT_PROVIDER` from here, so the
  // heuristic is: if the env-default model id looks like an
  // OpenRouter id (contains "/"), treat empty as openrouter; else
  // treat as gemini. Robust enough for the three providers we ship,
  // and will get refactored if a fourth lands.
  const emptyMapsTo: "openrouter" | "gemini" =
    envDefaultModelId.includes("/") ? "openrouter" : "gemini";
  const effectiveProvider: "openrouter" | "gemini" | "local" =
    override.provider === "local"
      ? "local"
      : override.provider === "openrouter"
        ? "openrouter"
        : override.provider === "gemini"
          ? "gemini"
          : emptyMapsTo;

  // Stale model id, local side: developer chose a model name, then
  // unloaded it in LM Studio. We don't auto-clear it (the user might
  // reload it shortly), but we flag it red so they know to refresh +
  // repick.
  const localModelKnown =
    override.modelId === "" ||
    models.some((m) => m.id === override.modelId);

  // Stale model id, openrouter side: developer pinned a `:free` id
  // that OpenRouter has since deprecated, or an id that was never
  // free-tier. Same red-border treatment so the dev can re-pick.
  const orModelKnown =
    override.modelId === "" ||
    orModels.some((m) => m.id === override.modelId);

  // Stale model id, gemini side: developer pinned a Gemma/Gemini id
  // that Google has rolled off the API. Same flag.
  const gmModelKnown =
    override.modelId === "" ||
    gmModels.some((m) => m.id === override.modelId);

  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2">
        <span className="flex items-center gap-2 text-[0.7rem] text-mca-text">
          {label}
          <LastTurnTag ms={lastTurnMs} />
        </span>
        <Segment
          value={effectiveProvider}
          onChange={(v) => {
            if (v === emptyMapsTo) {
              // Switching to whatever the production default is
              // means "go back to env-default for this role"; clear
              // the override entirely.
              onChange({ provider: "", modelId: "" });
            } else if (v === "openrouter") {
              onChange({ provider: "openrouter", modelId: "" });
            } else if (v === "gemini") {
              onChange({ provider: "gemini", modelId: "" });
            } else {
              // Switching to local preserves any existing local
              // modelId; if the user had pinned a non-local id, the
              // local-side stale-id handling will flag it red.
              onChange({ provider: "local", modelId: override.modelId });
            }
          }}
        />
      </div>
      <p className="text-[0.6rem] text-mca-muted leading-snug">{hint}</p>
      {effectiveProvider === "gemini" ? (
        // Gemini / Gemma side. The dropdown is populated by
        // `/agent/gemini/models` proxied through agent-service so the
        // GEMINI_API_KEY never crosses the browser boundary. Picking
        // a model id sends `provider: "gemini" / modelId: "<id>"` on
        // the next request. Empty id falls back to the env default
        // (resolved through `AGENT_DEFAULT_PROVIDER` + `AGENT_*_MODEL`).
        <select
          className={`w-full text-[0.7rem] bg-mca-bg border rounded px-2 py-1 focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent ${
            gmModelKnown ? "border-mca-border" : "border-rose-500"
          }`}
          value={override.modelId}
          onChange={(e) => {
            const id = e.target.value;
            onChange(
              id === ""
                ? emptyMapsTo === "gemini"
                  ? { provider: "", modelId: "" }
                  : { provider: "gemini", modelId: "" }
                : { provider: "gemini", modelId: id },
            );
          }}
          disabled={gmReachable === false && gmModels.length === 0}
          aria-label={`gemini / gemma model id for ${role}`}
        >
          <option value="">
            {gmReachable === false
              ? "gemini key missing or unreachable"
              : emptyMapsTo === "gemini" && envDefaultModelId
                ? `${envDefaultModelId} (env default)`
                : "(env default)"}
          </option>
          {!gmModelKnown && override.modelId ? (
            <option value={override.modelId}>
              {override.modelId} (no longer listed)
            </option>
          ) : null}
          {gmModels.map((m) => (
            <option key={m.id} value={m.id}>
              {m.id}
            </option>
          ))}
        </select>
      ) : effectiveProvider === "local" ? (
        <select
          className={`w-full text-[0.7rem] bg-mca-bg border rounded px-2 py-1 focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent ${
            localModelKnown ? "border-mca-border" : "border-rose-500"
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
          {!localModelKnown && override.modelId ? (
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
        // OpenRouter side. Empty value clears the override only when
        // openrouter is the current production default; otherwise
        // it's just a "pick a model" placeholder, since shipping
        // `provider: "openrouter" / modelId: ""` while the env
        // model id is the gemini-shaped `gemma-4-31b-it` would route
        // a non-existent OpenRouter id and 4xx out.
        <select
          className={`w-full text-[0.7rem] bg-mca-bg border rounded px-2 py-1 focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent ${
            orModelKnown ? "border-mca-border" : "border-rose-500"
          }`}
          value={override.modelId}
          onChange={(e) => {
            const id = e.target.value;
            onChange(
              id === ""
                ? emptyMapsTo === "openrouter"
                  ? { provider: "", modelId: "" }
                  : { provider: "openrouter", modelId: "" }
                : { provider: "openrouter", modelId: id },
            );
          }}
          disabled={orReachable === false && orModels.length === 0}
          aria-label={`openrouter free-tier model id for ${role}`}
        >
          <option value="">
            {orReachable === false
              ? "openrouter unreachable"
              : emptyMapsTo === "openrouter" && envDefaultModelId
                ? `${envDefaultModelId} (env default)`
                : "select a :free model"}
          </option>
          {!orModelKnown && override.modelId ? (
            <option value={override.modelId}>
              {override.modelId} (not in :free list)
            </option>
          ) : null}
          {orModels.map((m) => (
            <option key={m.id} value={m.id}>
              {m.id}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

/**
 * Codex-runtime primary-role row. Two compact selects (model +
 * reasoning effort) sit where the pydantic-ai panel's provider
 * segments + model dropdown live. Empty value in either select
 * means "fall through to the env default", which the label echoes
 * back from `/agent/config/role-defaults`. Stale-pin red-border
 * treatment matches the pydantic-ai rows: if the user pinned a
 * model id that's no longer in the catalog (codex-cli pin bumped
 * and dropped that id), the select renders red so they re-pick.
 */
function CodexPrimaryRow({
  modelId,
  reasoningEffort,
  models,
  reachable,
  reasoningEfforts,
  envDefaultModel,
  envDefaultEffort,
  lastTurnMs,
  onModelChange,
  onEffortChange,
}: {
  modelId: string;
  reasoningEffort: string;
  models: CodexModel[];
  reachable: boolean | null;
  reasoningEfforts: string[];
  envDefaultModel: string;
  envDefaultEffort: string;
  lastTurnMs: number | null;
  onModelChange: (id: string) => void;
  onEffortChange: (effort: string) => void;
}) {
  const modelKnown =
    modelId === "" || models.some((m) => m.id === modelId);
  const effortKnown =
    reasoningEffort === "" || reasoningEfforts.includes(reasoningEffort);

  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2">
        <span className="flex items-center gap-2 text-[0.7rem] text-mca-text">
          primary (codex)
          <LastTurnTag ms={lastTurnMs} />
        </span>
      </div>
      <p className="text-[0.6rem] text-mca-muted leading-snug">
        the codex-cli agent that picks tools and writes the
        narrative. policy + judge below run via pydantic-ai.
      </p>
      <div className="flex gap-2">
        <select
          className={`flex-1 text-[0.7rem] bg-mca-bg border rounded px-2 py-1 focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent ${
            modelKnown ? "border-mca-border" : "border-rose-500"
          }`}
          value={modelId}
          onChange={(e) => onModelChange(e.target.value)}
          disabled={reachable === false && models.length === 0}
          aria-label="codex primary model id"
        >
          <option value="">
            {reachable === false
              ? "codex catalog unreachable"
              : envDefaultModel
                ? `${envDefaultModel} (env default)`
                : "(cli default)"}
          </option>
          {!modelKnown && modelId ? (
            <option value={modelId}>
              {modelId} (no longer listed)
            </option>
          ) : null}
          {models.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name || m.id}
            </option>
          ))}
        </select>
        <select
          className={`text-[0.7rem] bg-mca-bg border rounded px-2 py-1 focus:outline-none focus-visible:ring-1 focus-visible:ring-mca-accent ${
            effortKnown ? "border-mca-border" : "border-rose-500"
          }`}
          value={reasoningEffort}
          onChange={(e) => onEffortChange(e.target.value)}
          disabled={reachable === false && reasoningEfforts.length === 0}
          aria-label="codex reasoning effort"
        >
          <option value="">
            {envDefaultEffort
              ? `effort: ${envDefaultEffort} (env)`
              : "effort (cli default)"}
          </option>
          {!effortKnown && reasoningEffort ? (
            <option value={reasoningEffort}>
              effort: {reasoningEffort} (unknown)
            </option>
          ) : null}
          {reasoningEfforts.map((e) => (
            <option key={e} value={e}>
              effort: {e}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

/**
 * Compact "last turn: X.Xs" badge next to the role label. The
 * threshold colors match the per-attempt timeout budgets used by
 * `with_provider_retry` (75s primary, 30s policy gates, 20s repeat
 * detector); a value within 80% of any of those budgets renders amber
 * to flag "this turn was close to timing out, the model may be a
 * candidate to swap." Anything above the budget itself renders rose
 * (the call would have been retried, possibly aborted on the second
 * attempt).
 *
 * `null` ms means no turn has completed yet this session; we show
 * nothing rather than a 0.0s placeholder so the row stays clean
 * before the dev's first interaction. `0` ms means the role didn't
 * fire on the last turn (judge today, always); we show a muted
 * "idle" hint so the row doesn't suggest "0s = blazing fast" when
 * the right reading is "didn't run."
 */
function LastTurnTag({ ms }: { ms: number | null }) {
  if (ms === null) return null;
  if (ms === 0) {
    return (
      <span className="text-[0.55rem] uppercase tracking-[1px] text-mca-dim">
        idle
      </span>
    );
  }
  const seconds = ms / 1000;
  // Pick the closer-to-the-edge tone: any role over 80% of its
  // tightest expected budget is worth flagging. We use 60s as the
  // amber threshold (= 80% of the 75s primary budget; the policy
  // bucket sums multiple calls and rarely approaches its 30s+30s+20s
  // headroom in practice).
  const tone =
    seconds >= 75 ? "text-rose-400"
    : seconds >= 60 ? "text-amber-400"
    : "text-mca-muted";
  return (
    <span className={`text-[0.55rem] tabular-nums ${tone}`}>
      last {seconds.toFixed(1)}s
    </span>
  );
}

function Segment({
  value,
  onChange,
}: {
  value: "openrouter" | "gemini" | "local";
  onChange: (v: "openrouter" | "gemini" | "local") => void;
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
        active={value === "gemini"}
        onClick={() => onChange("gemini")}
      >
        gemini
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
