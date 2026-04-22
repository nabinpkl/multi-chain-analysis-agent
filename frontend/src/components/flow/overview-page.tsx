"use client";

import dynamic from "next/dynamic";
import { useOverview } from "@/hooks/use-overview";
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

export function OverviewPage() {
  const query = useOverview("1h");
  const data = query.data;

  const txPerSec = data?.stats.tx_per_sec_recent ?? 0;
  const windowLabel = data?.window.label ?? "1h";

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem)] min-h-[640px]">
      <header className="flex items-center justify-between px-6 py-3 border-b border-mca-border bg-mca-bg">
        <h1 className="text-[0.7rem] uppercase tracking-[2px] text-mca-muted">
          Solana SOL flow · last {windowLabel}
        </h1>
        <LiveIndicator txPerSec={txPerSec} active={!!data} />
      </header>

      <div className="flex-1 flex min-h-0">
        <div className="flex-1 relative bg-mca-bg">
          {data && data.nodes.length > 0 ? (
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
            {data ? (
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
          {data
            ? `${formatInt(data.nodes.length)} wallets · ${formatInt(data.edges.length)} connections`
            : "—"}
        </span>
        <span>
          {data
            ? `updated ${relativeTime(data.generated_at)}`
            : query.isFetching
              ? "updating"
              : ""}
        </span>
      </footer>
    </div>
  );
}
