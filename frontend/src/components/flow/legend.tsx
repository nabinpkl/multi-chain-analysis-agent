"use client";

import { CLUSTER_COLORS, LONELY_COLOR } from "@/lib/cluster-colors";

export function Legend() {
  return (
    <section className="space-y-3">
      <p className="text-[0.65rem] uppercase tracking-[2px] text-mca-muted">
        Legend
      </p>

      <div className="space-y-2 text-[0.78rem] text-mca-dim">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-0.5">
            {CLUSTER_COLORS.map((c) => (
              <span
                key={c}
                className="w-2 h-2 rounded-full"
                style={{ backgroundColor: c }}
              />
            ))}
          </div>
          <span>clusters (connected wallets)</span>
        </div>

        <div className="flex items-center gap-3">
          <span
            className="w-2 h-2 rounded-full"
            style={{ backgroundColor: LONELY_COLOR }}
          />
          <span>lonely whale (no top flows)</span>
        </div>

        <div className="flex items-center gap-3">
          <span className="flex items-center gap-1">
            <span
              className="w-1.5 h-1.5 rounded-full bg-mca-text/60"
            />
            <span className="w-2.5 h-2.5 rounded-full bg-mca-text/60" />
            <span className="w-3.5 h-3.5 rounded-full bg-mca-text/60" />
          </span>
          <span>size = wallet volume (log)</span>
        </div>

        <div className="flex items-center gap-3">
          <span className="inline-block h-[1px] w-10 bg-mca-edge" />
          <span>edge = SOL flow between pair</span>
        </div>
      </div>
    </section>
  );
}
