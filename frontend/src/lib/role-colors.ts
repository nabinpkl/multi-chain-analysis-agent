import type { NodeRole } from "@/lib/role-detect";

/**
 * Fixed palette per node role. Used both by the Sigma renderer (via the
 * `rgb` field, since Sigma's WebGL color parser doesn't accept oklch)
 * and the sidebar legend (via the `oklch` field, per project color
 * convention). Single source of truth so the swatches in the legend
 * always match what's painted on the canvas.
 */
export const ROLE_PALETTE: Record<
  NodeRole,
  { rgb: string; oklch: string; label: string }
> = {
  "token-mint": {
    rgb: "rgb(230, 100, 180)",
    oklch: "oklch(0.62 0.22 340)",
    label: "Token mint",
  },
  "tip-account": {
    rgb: "rgb(255, 140, 60)",
    oklch: "oklch(0.74 0.17 50)",
    label: "Jito tip",
  },
  "mev-searcher": {
    rgb: "rgb(80, 200, 220)",
    oklch: "oklch(0.78 0.13 215)",
    label: "MEV searcher",
  },
  "flow-hub": {
    rgb: "rgb(120, 210, 110)",
    oklch: "oklch(0.79 0.16 145)",
    label: "Flow hub",
  },
  whale: {
    rgb: "rgb(240, 200, 90)",
    oklch: "oklch(0.84 0.14 92)",
    label: "Whale",
  },
  "mpc-member": {
    rgb: "rgb(180, 120, 230)",
    oklch: "oklch(0.65 0.20 305)",
    label: "MPC member",
  },
  normal: {
    rgb: "rgb(217, 200, 169)",
    oklch: "oklch(0.83 0.04 85)",
    label: "Other wallet",
  },
};

export function colorForRole(role: NodeRole): string {
  return ROLE_PALETTE[role].rgb;
}

/**
 * Edge color palette. Same alpha (0.25) as the regular transfer
 * edge so density still emerges via overdraw, just hue-shifted to
 * communicate kind. Used by the raw-stream `applyEdge` to color
 * mint and burn edges distinctly from regular wallet-to-wallet
 * transfers.
 */
export const EDGE_PALETTE = {
  transfer: {
    rgb: "rgba(200, 210, 235, 0.25)",
    oklch: "oklch(0.85 0.04 250 / 0.7)",
    label: "Transfer",
  },
  mint: {
    rgb: "rgba(140, 230, 100, 0.25)",
    oklch: "oklch(0.83 0.20 135 / 0.7)",
    label: "Mint (token created)",
  },
  burn: {
    rgb: "rgba(255, 100, 80, 0.25)",
    oklch: "oklch(0.66 0.22 28 / 0.7)",
    label: "Burn (token destroyed)",
  },
} as const;

export type EdgeKind = keyof typeof EDGE_PALETTE;

export function colorForEdgeKind(kind: EdgeKind): string {
  return EDGE_PALETTE[kind].rgb;
}
