"use client";

import dynamic from "next/dynamic";
import { useMemo } from "react";
import { useRawStream, type RoleSummary } from "@/hooks/use-raw-stream";
import { formatInt } from "@/lib/format";
import { EDGE_PALETTE, ROLE_PALETTE } from "@/lib/role-colors";
import type { NodeRole } from "@/lib/role-detect";
import { LiveIndicator } from "@/components/flow/live-indicator";

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

function fmtTime(ts: number): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString();
}

export function GraphPage() {
  const { graph, status, roleSummary } = useRawStream();
  const from = useMemo(() => fmtTime(status.firstBlockTime), [status.firstBlockTime]);
  const to = useMemo(() => fmtTime(status.latestBlockTime), [status.latestBlockTime]);

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem)] min-h-[640px]">
      <header className="flex items-center justify-between px-6 py-3 border-b border-mca-border bg-mca-bg">
        <h1 className="text-[0.7rem] uppercase tracking-[2px] text-mca-muted tabular-nums">
          Solana SOL flow · {from} - {to}
        </h1>
        <LiveIndicator active={status.connected} />
      </header>

      <div className="flex-1 flex min-h-0">
        <div className="flex-1 relative bg-mca-bg">
          <RawGraphCanvas graph={graph} />
        </div>

        <aside className="w-[320px] shrink-0 border-l border-mca-border bg-mca-surface/40 overflow-y-auto">
          <div className="p-6 space-y-8">
            <RawPanel status={status} roleSummary={roleSummary} />
          </div>
        </aside>
      </div>

      <footer className="px-6 py-2 border-t border-mca-border bg-mca-bg text-[0.7rem] uppercase tracking-[1.5px] text-mca-muted flex items-center justify-between gap-4">
        <span className="tabular-nums">
          {formatInt(status.nodeCount)} wallets · {formatInt(status.edgeCount)} edges
          {status.lagged > 0 ? ` · ${formatInt(status.lagged)} missed` : ""}
        </span>
        <span>{status.connected ? "live" : "disconnected"}</span>
      </footer>
    </div>
  );
}

function RawPanel({
  status,
  roleSummary,
}: {
  status: {
    connected: boolean;
    edgeCount: number;
    nodeCount: number;
    lagged: number;
  };
  roleSummary: RoleSummary;
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
      <RoleLegend roleSummary={roleSummary} />
      <p className="text-mca-dim text-xs leading-relaxed pt-2 border-t border-mca-border">
        every Solana transaction we see, painted live. same-pair edges thicken
        with each tx; hubs emerge naturally because busy wallets appear as
        endpoints more often. nothing is filtered: dust pairs, one-off
        transfers, and singletons all show up the moment they hit the stream.
      </p>
    </div>
  );
}

const ROLE_ORDER: NodeRole[] = [
  "token-mint",
  "tip-account",
  "mev-searcher",
  "multi-hub",
  "sol-hub",
  "spl-hub",
  "whale",
  "mpc-member",
  "normal",
];

const EDGE_KIND_ORDER = ["transfer", "mint", "burn"] as const;

function RoleLegend({ roleSummary }: { roleSummary: RoleSummary }) {
  return (
    <div className="pt-2 border-t border-mca-border space-y-4">
      <div>
        <div className="text-[0.65rem] uppercase tracking-[1.5px] text-mca-muted mb-2">
          nodes
        </div>
        <ul className="space-y-1.5">
          {ROLE_ORDER.map((role) => {
            const palette = ROLE_PALETTE[role];
            const count = roleSummary[role] ?? 0;
            return (
              <li
                key={role}
                className="flex items-center justify-between gap-3 text-xs"
              >
                <span className="flex items-center gap-2 min-w-0">
                  <span
                    className="size-2.5 rounded-full shrink-0"
                    style={{ backgroundColor: palette.oklch }}
                  />
                  <span className="text-mca-text truncate">{palette.label}</span>
                </span>
                <span className="tabular-nums text-mca-dim">
                  {formatInt(count)}
                </span>
              </li>
            );
          })}
        </ul>
      </div>
      <div>
        <div className="text-[0.65rem] uppercase tracking-[1.5px] text-mca-muted mb-2">
          edges
        </div>
        <ul className="space-y-1.5">
          {EDGE_KIND_ORDER.map((kind) => {
            const palette = EDGE_PALETTE[kind];
            return (
              <li key={kind} className="flex items-center gap-2 text-xs">
                <span
                  className="h-[2px] w-5 shrink-0 rounded-full"
                  style={{ backgroundColor: palette.oklch }}
                />
                <span className="text-mca-text">{palette.label}</span>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
