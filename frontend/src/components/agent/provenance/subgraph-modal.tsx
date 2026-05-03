"use client";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { SubgraphSlice } from "@/lib/wire/multichain/wire/shared/v1/subgraph_pb";

/**
 * Modal canvas for historical subgraphs (ship 5 populates `slice`).
 * v0 renders a placeholder body if a slice is ever attached; the
 * dialog surface itself exists so ship 5 just fills the renderer.
 */
export function SubgraphModal({
  open,
  onOpenChange,
  slice,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  slice: SubgraphSlice | null;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Historical subgraph</DialogTitle>
        </DialogHeader>
        {slice ? (
          <div className="text-xs text-mca-muted space-y-2">
            <p>
              {slice.nodes.length} wallets and {slice.edges.length} edges
              {slice.timeRange
                ? ` between ${new Date(slice.timeRange.fromS * 1000).toLocaleString()} and ${new Date(
                    slice.timeRange.toS * 1000,
                  ).toLocaleString()}`
                : ""}
              .
            </p>
            <p className="text-mca-dim">
              Modal canvas rendering lands in ship 5.
            </p>
          </div>
        ) : (
          <p className="text-xs text-mca-dim">
            No subgraph slice attached. Historical claims (ship 5) carry
            slices here.
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}
