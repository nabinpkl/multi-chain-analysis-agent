"use client";

import dynamic from "next/dynamic";
import { useMemo, useState } from "react";
import { useOverview } from "@/hooks/use-overview";
import type { OverviewWindow } from "@/lib/api";
import { formatInt, formatSol, relativeTime, truncateAddress } from "@/lib/format";
import { Legend } from "@/components/flow/legend";
import { StatsPanel } from "@/components/flow/stats-panel";
import { TopWalletCard } from "@/components/flow/top-wallet-card";
import { WindowSelect } from "@/components/flow/window-select";

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

const WINDOW_LABELS: Record<OverviewWindow, string> = {
  "15m": "last 15 minutes",
  "1h": "last hour",
  "6h": "last 6 hours",
  "24h": "last 24 hours",
};

export function OverviewPage() {
  const [window, setWindow] = useState<OverviewWindow>("24h");
  const query = useOverview(window);
  const data = query.data;

  const headline = useMemo(() => {
    if (!data) return null;
    const { stats } = data;
    const clusterCount = new Set(
      data.nodes.filter((n) => n.component !== null).map((n) => n.component),
    ).size;
    return {
      volume: formatSol(stats.total_volume_sol),
      txs: formatInt(stats.total_txs),
      wallets: formatInt(stats.unique_wallets),
      clusters: clusterCount,
      topWallet: stats.top_wallet,
      topVolume:
        stats.top_wallet_volume_sol !== null
          ? formatSol(stats.top_wallet_volume_sol)
          : null,
    };
  }, [data]);

  return (
    <div className="flex flex-col h-[calc(100vh-3.5rem)] min-h-[640px]">
      <header className="flex items-center justify-between px-6 py-4 border-b border-mca-border bg-mca-bg">
        <div className="min-w-0 flex-1 pr-6">
          <h1 className="text-[0.65rem] uppercase tracking-[2px] text-mca-muted mb-1">
            Solana SOL flow · {WINDOW_LABELS[window]}
          </h1>
          <p className="text-mca-text text-sm sm:text-base leading-snug">
            {headline ? (
              <>
                <span className="text-mca-accent font-medium tabular-nums">
                  {headline.volume} SOL
                </span>{" "}
                moved across{" "}
                <span className="text-mca-text tabular-nums">
                  {headline.txs}
                </span>{" "}
                transactions.{" "}
                {headline.topWallet && headline.topVolume && (
                  <>
                    Top wallet{" "}
                    <span className="font-mono text-mca-dim">
                      {truncateAddress(headline.topWallet)}
                    </span>{" "}
                    moved{" "}
                    <span className="tabular-nums">{headline.topVolume} SOL</span>.
                  </>
                )}
              </>
            ) : query.isLoading ? (
              <span className="text-mca-dim">loading…</span>
            ) : query.isError ? (
              <span className="text-mca-dim">
                backend unreachable. is the api running at{" "}
                {process.env.NEXT_PUBLIC_API_URL || "http://localhost:8002"}?
              </span>
            ) : null}
          </p>
        </div>
        <WindowSelect value={window} onChange={setWindow} />
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
            {data && (
              <>
                <StatsPanel stats={data.stats} windowLabel={window} />
                <TopWalletCard stats={data.stats} />
                <Legend />
              </>
            )}
          </div>
        </aside>
      </div>

      <footer className="px-6 py-2 border-t border-mca-border bg-mca-bg text-[0.7rem] uppercase tracking-[1.5px] text-mca-muted flex items-center justify-between gap-4">
        <span className="tabular-nums">
          {data
            ? `${formatInt(data.nodes.length)} nodes · ${formatInt(data.edges.length)} edges`
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
