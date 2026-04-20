"use client";

import type { StatsView } from "@/lib/api";
import { formatSol, truncateAddress } from "@/lib/format";

interface TopWalletCardProps {
  stats: StatsView;
}

export function TopWalletCard({ stats }: TopWalletCardProps) {
  if (!stats.top_wallet || stats.top_wallet_volume_sol === null) {
    return null;
  }

  return (
    <section className="rounded-sm border border-mca-border bg-mca-surface/60 p-4 space-y-2">
      <p className="text-[0.65rem] uppercase tracking-[2px] text-mca-muted">
        Top wallet
      </p>
      <p
        className="font-mono text-sm text-mca-text break-all"
        title={stats.top_wallet}
      >
        {truncateAddress(stats.top_wallet, 6, 6)}
      </p>
      <p className="text-mca-dim text-sm tabular-nums">
        {formatSol(stats.top_wallet_volume_sol)} SOL moved
      </p>
    </section>
  );
}
