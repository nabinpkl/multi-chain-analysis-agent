"use client";

import type { StatsView } from "@/lib/api";
import { formatInt, formatSol } from "@/lib/format";

interface StatsPanelProps {
  stats: StatsView;
}

export function StatsPanel({ stats }: StatsPanelProps) {
  return (
    <section className="space-y-5">
      <StatRow
        label="Volume"
        value={`${formatSol(stats.total_volume_sol)} SOL`}
      />
      <StatRow label="Transactions" value={formatInt(stats.total_txs)} />
      <StatRow label="Unique wallets" value={formatInt(stats.unique_wallets)} />
    </section>
  );
}

function StatRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="space-y-1">
      <p className="text-[0.65rem] uppercase tracking-[2px] text-mca-muted">
        {label}
      </p>
      <p className="font-sans text-2xl text-mca-text tabular-nums">{value}</p>
    </div>
  );
}
