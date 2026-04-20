export type OverviewWindow = "15m" | "1h" | "6h" | "24h";

export interface WindowView {
  from: number;
  to: number;
  label: string;
}

export interface StatsView {
  total_volume_sol: number;
  total_txs: number;
  unique_wallets: number;
  top_wallet: string | null;
  top_wallet_volume_sol: number | null;
}

export interface NodeView {
  id: string;
  volume_sol: number;
  component: number | null;
}

export interface EdgeView {
  from: string;
  to: string;
  volume_sol: number;
  tx_count: number;
}

export interface OverviewResponse {
  window: WindowView;
  stats: StatsView;
  nodes: NodeView[];
  edges: EdgeView[];
  generated_at: number;
  cache_ttl_secs: number;
}

const DEFAULT_API_URL = "http://localhost:8002";

function apiUrl(): string {
  return process.env.NEXT_PUBLIC_API_URL || DEFAULT_API_URL;
}

export async function fetchOverview(
  window: OverviewWindow,
  signal?: AbortSignal,
): Promise<OverviewResponse> {
  const url = new URL("/graph/overview", apiUrl());
  url.searchParams.set("window", window);

  const res = await fetch(url.toString(), { signal });
  if (!res.ok) {
    throw new Error(`overview request failed: ${res.status}`);
  }
  return res.json();
}
