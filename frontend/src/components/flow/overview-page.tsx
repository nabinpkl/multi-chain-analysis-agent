"use client";

import dynamic from "next/dynamic";
import { useState } from "react";
import { useOverview } from "@/hooks/use-overview";
import { useRawStream } from "@/hooks/use-raw-stream";
import { formatInt, relativeTime } from "@/lib/format";
import { Legend } from "@/components/flow/legend";
import { LiveIndicator } from "@/components/flow/live-indicator";
import { StatsPanel } from "@/components/flow/stats-panel";
import { TopWalletCard } from "@/components/flow/top-wallet-card";

const GraphCanvas = dynamic(
  () => import("@/components/flow/graph-canvas").then((m) => m.GraphCanvas),
  {
    ssr: false,
    loading: () => (
      <div className="w-full h-full flex items-center justify-center text-mca-dim text-sm">
        loading graph engine…
      </div>
    ),
  },
);

const RawGraphCanvas = dynamic(
  () =>
    import("@/components/flow/raw-graph-canvas").then((m) => m.RawGraphCanvas),
  {
    ssr: false,
    loading: () => (
      <div className="w-full h-full flex items-center justify-center text-mca-dim text-sm">
        loading graph engine…
      </div>
    ),
  },
);

type Mode = "overview" | "raw";

export function OverviewPage() {
  const [mode, setMode] = useState<Mode>("raw");
  const query = useOverview("1h");
  const raw = useRawStream();
  const data = query.data;

  const txPerSec = data?.stats.tx_per_sec_recent ?? 0;
  const windowLabel = data?.window.label ?? "1h";

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem)] min-h-[640px]">
      <header className="flex items-center justify-between px-6 py-3 border-b border-mca-border bg-mca-bg">
        <h1 className="text-[0.7rem] uppercase tracking-[2px] text-mca-muted">
          Solana SOL flow{" "}
          {mode === "overview" ? `· last ${windowLabel}` : "· raw stream"}
        </h1>
        <div className="flex items-center gap-3">
          <ModeToggle mode={mode} onChange={setMode} />
          <LiveIndicator
            txPerSec={mode === "raw" ? 0 : txPerSec}
            active={mode === "raw" ? raw.status.connected : !!data}
          />
        </div>
      </header>

      <div className="flex-1 flex min-h-0">
        <div className="flex-1 relative bg-mca-bg">
          {mode === "raw" ? (
            <RawGraphCanvas graph={raw.graph} />
          ) : data && data.nodes.length > 0 ? (
            <GraphCanvas nodes={data.nodes} edges={data.edges} />
          ) : (
            <div className="absolute inset-0 flex items-center justify-center text-mca-dim text-sm">
              {query.isLoading
                ? "fetching graph…"
                : query.isError
                  ? "no data"
                  : "no flows in this window yet"}
            </div>
          )}
        </div>

        <aside className="w-[320px] shrink-0 border-l border-mca-border bg-mca-surface/40 overflow-y-auto">
          <div className="p-6 space-y-8">
            {mode === "raw" ? (
              <RawPanel status={raw.status} />
            ) : data ? (
              <>
                <StatsPanel stats={data.stats} />
                <TopWalletCard stats={data.stats} />
                <Legend />
              </>
            ) : query.isError ? (
              <p className="text-mca-dim text-sm">
                backend unreachable. is the api running at{" "}
                {process.env.NEXT_PUBLIC_API_URL || "http://localhost:8002"}?
              </p>
            ) : (
              <p className="text-mca-dim text-sm">loading…</p>
            )}
          </div>
        </aside>
      </div>

      <footer className="px-6 py-2 border-t border-mca-border bg-mca-bg text-[0.7rem] uppercase tracking-[1.5px] text-mca-muted flex items-center justify-between gap-4">
        <span className="tabular-nums">
          {mode === "raw"
            ? `${formatInt(raw.status.nodeCount)} wallets · ${formatInt(raw.status.edgeCount)} edges${raw.status.lagged > 0 ? ` · ${formatInt(raw.status.lagged)} missed` : ""}`
            : data
              ? `${formatInt(data.nodes.length)} wallets · ${formatInt(data.edges.length)} connections`
              : ""}
        </span>
        <span>
          {mode === "raw"
            ? raw.status.connected
              ? "live"
              : "disconnected"
            : data
              ? `updated ${relativeTime(data.generated_at)}`
              : query.isFetching
                ? "updating"
                : ""}
        </span>
      </footer>
    </div>
  );
}

function ModeToggle({
  mode,
  onChange,
}: {
  mode: Mode;
  onChange: (m: Mode) => void;
}) {
  const base =
    "px-2 py-1 text-[0.65rem] uppercase tracking-[1.5px] rounded-sm transition-colors";
  return (
    <div className="flex gap-1 bg-mca-surface/60 border border-mca-border rounded-sm p-[2px]">
      <button
        type="button"
        className={`${base} ${mode === "raw" ? "bg-mca-accent/20 text-mca-text" : "text-mca-muted hover:text-mca-text"}`}
        onClick={() => onChange("raw")}
      >
        raw
      </button>
      <button
        type="button"
        className={`${base} ${mode === "overview" ? "bg-mca-accent/20 text-mca-text" : "text-mca-muted hover:text-mca-text"}`}
        onClick={() => onChange("overview")}
      >
        overview
      </button>
    </div>
  );
}

function RawPanel({
  status,
}: {
  status: {
    connected: boolean;
    edgeCount: number;
    nodeCount: number;
    lagged: number;
    filtered: number;
  };
}) {
  return (
    <div className="space-y-4 text-sm">
      <div>
        <div className="text-[0.65rem] uppercase tracking-[1.5px] text-mca-muted mb-1">
          status
        </div>
        <div className={status.connected ? "text-mca-text" : "text-mca-dim"}>
          {status.connected ? "streaming" : "disconnected"}
        </div>
      </div>
      <div>
        <div className="text-[0.65rem] uppercase tracking-[1.5px] text-mca-muted mb-1">
          wallets
        </div>
        <div className="tabular-nums text-mca-text">
          {formatInt(status.nodeCount)}
        </div>
      </div>
      <div>
        <div className="text-[0.65rem] uppercase tracking-[1.5px] text-mca-muted mb-1">
          unique edges
        </div>
        <div className="tabular-nums text-mca-text">
          {formatInt(status.edgeCount)}
        </div>
      </div>
      {status.filtered > 0 ? (
        <div>
          <div className="text-[0.65rem] uppercase tracking-[1.5px] text-mca-muted mb-1">
            noise filtered
          </div>
          <div className="tabular-nums text-mca-dim">
            {formatInt(status.filtered)}
          </div>
        </div>
      ) : null}
      {status.lagged > 0 ? (
        <div>
          <div className="text-[0.65rem] uppercase tracking-[1.5px] text-mca-muted mb-1">
            missed
          </div>
          <div className="tabular-nums text-mca-accent">
            {formatInt(status.lagged)}
          </div>
        </div>
      ) : null}
      <p className="text-mca-dim text-xs leading-relaxed pt-2 border-t border-mca-border">
        every Solana transaction we see, painted live. same-pair edges thicken
        with each tx; hubs emerge naturally because busy wallets appear as
        endpoints more often. dust transfers between two brand-new wallets are
        filtered to cut one-off noise; anything touching a known wallet always
        shows.
      </p>
    </div>
  );
}
